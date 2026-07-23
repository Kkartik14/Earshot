"""Concurrency + lifecycle isolation for framework span routing.

These tests exercise the real OpenTelemetry SDK provider path. Before the
process-scoped router they FAILED: one recorder-bound processor was added per
session to the shared provider, so every recorder ingested every session's
spans (cross-session contamination) and processors/recorders were retained
forever.
"""

from __future__ import annotations

import gc
import os
import select
import signal
import threading
import weakref

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import use_span

from earshot.adapters import (
    LiveKitAdapter,
    PipecatAdapter,
)
from earshot.recorder import IncidentRecorder, RecorderConfig

pytestmark = pytest.mark.integration

SECRET = "SENTINEL-do-not-cross-sessions"


class _CollidingSpanIdGenerator(IdGenerator):
    """Independent traces deliberately reuse one span ID."""

    def __init__(self) -> None:
        self._next_trace_id = 100

    def generate_trace_id(self) -> int:
        self._next_trace_id += 1
        return self._next_trace_id

    def generate_span_id(self) -> int:
        return 42


class _BlockingEndProcessor(SpanProcessor):
    """Pause OTel before Earshot's later-registered processor sees an end."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def on_start(self, span, parent_context=None) -> None:
        del span, parent_context

    def on_end(self, span) -> None:
        del span
        self.started.set()
        assert self.release.wait(5)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def _recorder(*, session_id: str | None = None) -> IncidentRecorder:
    return IncidentRecorder(
        session_id=session_id,
        config=RecorderConfig(clock_domain_id="server-clock"),
    )


def _livekit_processor_count(provider: TracerProvider) -> int:
    active = provider._active_span_processor
    processors = getattr(active, "_span_processors", ())
    return sum(1 for p in processors if type(p).__name__ == "_EarshotRouterProcessor")


def _emit_livekit_span(provider: TracerProvider, *, room: str, span_id_box: list[int]) -> None:
    tracer = provider.get_tracer("livekit-agents")
    with tracer.start_as_current_span(
        "llm_node",
        attributes={"lk.room": room, "earshot.turn.id": room},
    ) as span:
        span_id_box.append(span.get_span_context().span_id)


def _span_ids(bundle) -> set[str]:
    return {op.span_id for op in bundle.profile.operations}


def test_single_session_routes_without_activation() -> None:
    """A lone session needs no baggage scope: the sole sink is unambiguous."""

    provider = TracerProvider()
    adapter = LiveKitAdapter(_recorder())
    adapter.attach_span_processor(provider)

    box: list[int] = []
    _emit_livekit_span(provider, room="solo", span_id_box=box)

    bundle = adapter.recorder.close()
    assert _span_ids(bundle) == {format(box[0], "016x")}
    adapter.detach()


def test_concurrent_sessions_do_not_cross_contaminate() -> None:
    """Concurrent sessions on one provider each keep only their own span."""

    provider = TracerProvider()
    n = 100
    adapters = [LiveKitAdapter(_recorder()) for _ in range(n)]
    handles = [a.attach_span_processor(provider) for a in adapters]
    span_ids: list[list[int]] = [[] for _ in range(n)]
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        barrier.wait()
        with handles[i].session_scope():
            _emit_livekit_span(provider, room=f"room-{i}", span_id_box=span_ids[i])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_cross = 0
    for i, adapter in enumerate(adapters):
        bundle = adapter.recorder.close()
        own = format(span_ids[i][0], "016x")
        ids = _span_ids(bundle)
        assert ids == {own}, f"session {i} saw {ids - {own}} foreign spans"
        total_cross += len(ids - {own})
    assert total_cross == 0


def test_duplicate_user_session_ids_keep_independent_active_routes() -> None:
    """A caller-supplied session ID is metadata, not a routing registration key."""

    provider = TracerProvider()
    first = LiveKitAdapter(_recorder(session_id="shared-room-name"))
    second = LiveKitAdapter(_recorder(session_id="shared-room-name"))
    first_handle = first.attach_span_processor(provider)
    second_handle = second.attach_span_processor(provider)
    first_ids: list[int] = []
    second_ids: list[int] = []

    with first_handle.session_scope():
        _emit_livekit_span(provider, room="first", span_id_box=first_ids)
    with second_handle.session_scope():
        _emit_livekit_span(provider, room="second", span_id_box=second_ids)

    # Closing the first registration must not deregister the second registration,
    # even though both incidents deliberately use the same public session ID.
    first_handle.close()
    with second_handle.session_scope():
        _emit_livekit_span(provider, room="second-late", span_id_box=second_ids)

    first_bundle = first.recorder.close()
    second_bundle = second.recorder.close()
    assert _span_ids(first_bundle) == {format(first_ids[0], "016x")}
    assert _span_ids(second_bundle) == {format(item, "016x") for item in second_ids}


def test_span_ending_after_detach_is_not_delivered_to_the_next_session() -> None:
    """A late end retains its retired route and never uses the sole-sink fallback."""

    provider = TracerProvider()
    retired = LiveKitAdapter(_recorder())
    retired_handle = retired.attach(provider)
    with retired_handle.session_scope():
        late_span = provider.get_tracer("livekit-agents").start_span(
            "llm_node",
            attributes={"lk.response.text": SECRET, "lk.room": "retired"},
        )

    retired.detach()
    current = LiveKitAdapter(_recorder())
    current.attach(provider)
    late_span.end()

    assert not retired_handle.status.active
    assert retired_handle.status.quarantined_span_count == 1
    assert _span_ids(retired.recorder.close()) == set()
    current_bundle = current.recorder.close()
    assert _span_ids(current_bundle) == set()
    assert SECRET not in current_bundle.model_dump_json()


def test_unscoped_span_ending_after_detach_is_not_delivered_to_next_session() -> None:
    """Sole-sink fallback at start still records ownership across detach."""

    provider = TracerProvider()
    retired = LiveKitAdapter(_recorder())
    retired_handle = retired.attach(provider)
    late_span = provider.get_tracer("livekit-agents").start_span(
        "llm_node",
        attributes={"lk.response.text": SECRET, "lk.room": "retired-unscoped"},
    )

    retired.detach()
    current = LiveKitAdapter(_recorder())
    current.attach(provider)
    late_span.end()

    assert retired_handle.status.quarantined_span_count == 1
    assert _span_ids(retired.recorder.close()) == set()
    current_bundle = current.recorder.close()
    assert _span_ids(current_bundle) == set()
    assert SECRET not in current_bundle.model_dump_json()


def test_evicted_span_ownership_never_falls_through_to_next_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity pressure degrades to quarantine instead of cross-session delivery."""

    from earshot.adapters import routing

    monkeypatch.setattr(routing, "_MAX_SPAN_STASH", 2)
    provider = TracerProvider()
    retired = LiveKitAdapter(_recorder())
    retired_handle = retired.attach(provider)
    tracer = provider.get_tracer("livekit-agents")
    with retired_handle.session_scope():
        late_span = tracer.start_span(
            "llm_node",
            attributes={"lk.response.text": SECRET, "lk.room": "retired-evicted"},
        )

    retired.detach()
    current = LiveKitAdapter(_recorder())
    current_handle = current.attach(provider)
    with current_handle.session_scope():
        current_spans = [
            tracer.start_span("llm_node", attributes={"lk.room": f"current-{index}"})
            for index in range(2)
        ]

    late_span.end()
    for span in current_spans:
        span.end()

    assert _span_ids(retired.recorder.close()) == set()
    current_bundle = current.recorder.close()
    expected_current_ids = {
        format(span.get_span_context().span_id, "016x") for span in current_spans
    }
    assert _span_ids(current_bundle) == expected_current_ids
    assert SECRET not in current_bundle.model_dump_json()


def test_unattributed_open_span_stays_quarantined_after_topology_changes() -> None:
    """An ambiguous start cannot become attributable merely because a sink closes."""

    provider = TracerProvider()
    first = LiveKitAdapter(_recorder())
    second = LiveKitAdapter(_recorder())
    first_handle = first.attach(provider)
    second_handle = second.attach(provider)
    late_span = provider.get_tracer("livekit-agents").start_span(
        "llm_node",
        attributes={"lk.response.text": SECRET, "lk.room": "ambiguous"},
    )

    second.detach()
    late_span.end()

    assert first_handle.status.quarantined_span_count == 1
    assert second_handle.status.quarantined_span_count == 0
    first_bundle = first.recorder.close()
    assert _span_ids(first_bundle) == set()
    assert SECRET not in first_bundle.model_dump_json()


def test_conflicting_trace_ownership_remains_ambiguous() -> None:
    """A/B/A observations never relearn a shared trace for hint-free routing."""

    provider = TracerProvider()
    first = LiveKitAdapter(_recorder())
    second = LiveKitAdapter(_recorder())
    first_handle = first.attach(provider)
    second_handle = second.attach(provider)
    root = provider.get_tracer("test-harness").start_span("shared-root")
    tracer = provider.get_tracer("livekit-agents")

    with use_span(root, end_on_exit=False):
        for handle, room in (
            (first_handle, "first"),
            (second_handle, "second"),
            (first_handle, "first-again"),
        ):
            with handle.session_scope():
                tracer.start_span("llm_node", attributes={"lk.room": room}).end()
        tracer.start_span(
            "llm_node",
            attributes={"lk.response.text": SECRET, "lk.room": "unscoped-child"},
        ).end()
    root.end()

    assert first_handle.status.quarantined_span_count == 1
    assert second_handle.status.quarantined_span_count == 1
    for adapter in (first, second):
        bundle = adapter.recorder.close()
        assert len(bundle.profile.operations) == (2 if adapter is first else 1)
        assert SECRET not in bundle.model_dump_json()


def test_stale_explicit_scope_cannot_fall_back_to_shared_trace_owner() -> None:
    """An unresolved explicit route stays unattributable for the span lifecycle."""

    provider = TracerProvider()
    first = LiveKitAdapter(_recorder())
    retired = LiveKitAdapter(_recorder())
    first_handle = first.attach(provider)
    retired_handle = retired.attach(provider)
    root = provider.get_tracer("test-harness").start_span("shared-root")
    tracer = provider.get_tracer("livekit-agents")

    with use_span(root, end_on_exit=False):
        with first_handle.session_scope():
            tracer.start_span("llm_node", attributes={"lk.room": "first"}).end()

        retired.detach()
        with retired_handle.session_scope():
            stale_span = tracer.start_span(
                "llm_node",
                attributes={"lk.response.text": SECRET, "lk.room": "retired"},
            )
        stale_span.end()
    root.end()

    assert first_handle.status.quarantined_span_count == 1
    assert retired_handle.status.quarantined_span_count == 0
    first_bundle = first.recorder.close()
    retired_bundle = retired.recorder.close()
    assert len(first_bundle.profile.operations) == 1
    assert _span_ids(retired_bundle) == set()
    assert SECRET not in first_bundle.model_dump_json()
    assert [
        (item.signal, item.availability, item.reason)
        for item in first_bundle.profile.coverage
        if item.signal == "livekit.span.routing"
    ] == [
        (
            "livekit.span.routing",
            "partial",
            "unattributed_span_quarantined",
        )
    ]


def test_evicted_unattributable_span_cannot_reenable_trace_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity eviction fails closed after an unresolved explicit route."""

    from earshot.adapters import routing

    monkeypatch.setattr(routing, "_MAX_SPAN_STASH", 1)
    provider = TracerProvider()
    first = LiveKitAdapter(_recorder())
    retired = LiveKitAdapter(_recorder())
    first_handle = first.attach(provider)
    retired_handle = retired.attach(provider)
    root = provider.get_tracer("test-harness").start_span("shared-root")
    tracer = provider.get_tracer("livekit-agents")

    with use_span(root, end_on_exit=False):
        with first_handle.session_scope():
            tracer.start_span("llm_node", attributes={"lk.room": "first"}).end()
        retired.detach()
        with retired_handle.session_scope():
            stale_span = tracer.start_span(
                "llm_node",
                attributes={"lk.response.text": SECRET, "lk.room": "retired"},
            )
        with first_handle.session_scope():
            current_span = tracer.start_span("llm_node", attributes={"lk.room": "current"})
        stale_span.end()
        current_span.end()
    root.end()

    assert first_handle.status.quarantined_span_count == 1
    first_bundle = first.recorder.close()
    assert len(first_bundle.profile.operations) == 2
    assert SECRET not in first_bundle.model_dump_json()
    assert _span_ids(retired.recorder.close()) == set()


def test_on_end_waiting_behind_detach_cannot_deliver_after_close_returns() -> None:
    """A concurrent OTel end observed after close is quarantined, never delivered."""

    provider = TracerProvider()
    blocker = _BlockingEndProcessor()
    provider.add_span_processor(blocker)
    retired = LiveKitAdapter(_recorder())
    retired_handle = retired.attach(provider)
    with retired_handle.session_scope():
        late_span = provider.get_tracer("livekit-agents").start_span(
            "llm_node",
            attributes={"lk.response.text": SECRET, "lk.room": "retired-race"},
        )

    end_errors: list[BaseException] = []

    def end_span() -> None:
        try:
            late_span.end()
        except BaseException as error:  # pragma: no cover - asserted below
            end_errors.append(error)

    end_thread = threading.Thread(target=end_span)
    end_thread.start()
    assert blocker.started.wait(5)
    try:
        retired.detach()
        assert not retired_handle.status.active
        current = LiveKitAdapter(_recorder())
        current.attach(provider)
    finally:
        blocker.release.set()
        end_thread.join(5)

    assert not end_thread.is_alive()
    assert end_errors == []
    assert retired_handle.status.quarantined_span_count == 1
    assert _span_ids(retired.recorder.close()) == set()
    current_bundle = current.recorder.close()
    assert _span_ids(current_bundle) == set()
    assert SECRET not in current_bundle.model_dump_json()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork hooks",
)
def test_fork_retires_inherited_routes_before_child_reattaches() -> None:
    """A child starts with no active inherited sink and can attach independently."""

    provider = TracerProvider(shutdown_on_exit=False)
    inherited = LiveKitAdapter(_recorder())
    inherited_handle = inherited.attach(provider)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in the parent
        exit_code = 0
        try:
            os.close(read_fd)
            child = LiveKitAdapter(_recorder())
            child_handle = child.attach(provider)
            with child_handle.session_scope():
                _emit_livekit_span(provider, room="child", span_id_box=[])
            child_bundle = child.recorder.close()
            inherited_bundle = inherited.recorder.close()
            result = ",".join(
                (
                    str(inherited_handle.status.active),
                    str(len(child_bundle.profile.operations)),
                    str(len(inherited_bundle.profile.operations)),
                )
            )
            os.write(write_fd, result.encode())
            child.detach()
        except BaseException:
            exit_code = 1
        finally:
            os.close(write_fd)
            os._exit(exit_code)

    os.close(write_fd)
    try:
        readable, _, _ = select.select([read_fd], [], [], 5)
        assert readable, "forked routing child did not make progress"
        inherited_active, child_operations, inherited_operations = (
            os.read(read_fd, 256).decode().split(",")
        )
        assert inherited_active == "False"
        assert child_operations == "1"
        assert inherited_operations == "0"
        waited_pid, wait_status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(wait_status) == 0
    finally:
        os.close(read_fd)
        inherited.detach()
        inherited.recorder.close()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork hooks",
)
@pytest.mark.parametrize("held_lock", ["registry", "router", "sink"])
def test_fork_reset_never_acquires_a_lock_held_by_a_vanished_thread(held_lock: str) -> None:
    """The child replaces inherited locks before it touches routing state."""

    from earshot.adapters import routing

    provider = TracerProvider(shutdown_on_exit=False)
    inherited = LiveKitAdapter(_recorder())
    inherited_handle = inherited.attach(provider)
    locks = {
        "registry": routing._registry_lock,
        "router": inherited_handle._router._lock,
        "sink": inherited_handle._sink._lock,
    }
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with locks[held_lock]:
            acquired.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(5)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in the parent
        try:
            os.close(read_fd)
            os.write(write_fd, str(inherited_handle.status.active).encode())
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    child_reaped = False
    try:
        readable, _, _ = select.select([read_fd], [], [], 3)
        if not readable:
            os.kill(child_pid, signal.SIGKILL)
        waited_pid, wait_status = os.waitpid(child_pid, 0)
        child_reaped = True
        assert readable, f"child deadlocked while {held_lock} lock was inherited"
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(wait_status) == 0
        assert os.read(read_fd, 16) == b"False"
    finally:
        if not child_reaped:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
        os.close(read_fd)
        release.set()
        holder.join(5)
        inherited.detach()
        inherited.recorder.close()


def test_retired_route_does_not_retain_the_adapter() -> None:
    """Late-span safety keeps only content-free routing state after detach."""

    provider = TracerProvider()
    adapter = LiveKitAdapter(_recorder())
    adapter_ref = weakref.ref(adapter)
    handle = adapter.attach(provider)
    with handle.session_scope():
        late_span = provider.get_tracer("livekit-agents").start_span("llm_node")

    adapter.detach()
    del adapter
    gc.collect()
    assert adapter_ref() is None
    late_span.end()


def test_overlapping_traces_with_the_same_span_id_do_not_collide() -> None:
    """Routing identity is the OTel (trace_id, span_id) pair, not span_id alone."""

    provider = TracerProvider(id_generator=_CollidingSpanIdGenerator())
    first = LiveKitAdapter(_recorder())
    second = LiveKitAdapter(_recorder())
    first_handle = first.attach(provider)
    second_handle = second.attach(provider)
    tracer = provider.get_tracer("livekit-agents")

    # Keep both spans open so a bare span-ID map would overwrite the first route.
    with first_handle.session_scope():
        first_span = tracer.start_span("llm_node", attributes={"lk.room": "first"})
    with second_handle.session_scope():
        second_span = tracer.start_span("llm_node", attributes={"lk.room": "second"})
    first_span.end()
    second_span.end()

    assert _span_ids(first.recorder.close()) == {"000000000000002a"}
    assert _span_ids(second.recorder.close()) == {"000000000000002a"}


def test_one_router_processor_per_provider() -> None:
    """Attaching many sessions installs exactly one processor, not one each."""

    provider = TracerProvider()
    adapters = [LiveKitAdapter(_recorder()) for _ in range(50)]
    for a in adapters:
        a.attach_span_processor(provider)
    assert _livekit_processor_count(provider) == 1


@pytest.mark.parametrize("adapter_type", [LiveKitAdapter, PipecatAdapter])
def test_reattach_replaces_the_adapter_registration(adapter_type) -> None:
    """An adapter owns at most one active routing registration."""

    provider = TracerProvider()
    adapter = adapter_type(_recorder())
    original = adapter.attach(provider)
    replacement = adapter.attach(provider)

    assert not original.status.active
    assert replacement.status.active
    adapter.detach()
    assert not replacement.status.active


def test_detach_releases_routing_state() -> None:
    """Sequential sessions release their sink so routing state stays bounded."""

    provider = TracerProvider()
    router = None
    for _ in range(10_000):
        adapter = LiveKitAdapter(_recorder())
        handle = adapter.attach_span_processor(provider)
        router = handle._router
        with handle.session_scope():
            _emit_livekit_span(provider, room="seq", span_id_box=[])
        adapter.recorder.close()
        adapter.detach()

    assert _livekit_processor_count(provider) == 1
    assert router is not None
    assert router.sink_count == 0
    assert router.routing_state_size == 0


def test_unattributed_span_with_multiple_sessions_is_quarantined() -> None:
    """A dropped span is content-free but visible in every affected incident."""

    provider = TracerProvider()
    a1 = LiveKitAdapter(_recorder())
    a2 = LiveKitAdapter(_recorder())
    h1 = a1.attach_span_processor(provider)
    h2 = a2.attach_span_processor(provider)

    # No session_scope, no earshot conversation context, fresh trace.
    tracer = provider.get_tracer("livekit-agents")
    with tracer.start_as_current_span(
        "llm_node",
        attributes={"lk.response.text": SECRET, "lk.room": "orphan"},
    ):
        pass

    assert h1.status.active
    assert h2.status.active
    assert h1.status.quarantined_span_count == 1
    assert h2.status.quarantined_span_count == 1
    b1 = a1.recorder.close()
    b2 = a2.recorder.close()
    assert _span_ids(b1) == set()
    assert _span_ids(b2) == set()
    for bundle in (b1, b2):
        assert [
            (item.signal, item.availability, item.reason)
            for item in bundle.profile.coverage
            if item.signal == "livekit.span.routing"
        ] == [
            (
                "livekit.span.routing",
                "partial",
                "unattributed_span_quarantined",
            )
        ]
        assert SECRET not in bundle.model_dump_json()


def test_privacy_sentinel_not_leaked_across_concurrent_sessions() -> None:
    """A secret in one session's span never appears in another's bundle."""

    provider = TracerProvider()
    secret_adapter = LiveKitAdapter(_recorder())
    clean_adapter = LiveKitAdapter(_recorder())
    secret_handle = secret_adapter.attach_span_processor(provider)
    clean_handle = clean_adapter.attach_span_processor(provider)

    def emit_secret() -> None:
        with secret_handle.session_scope():
            tracer = provider.get_tracer("livekit-agents")
            with tracer.start_as_current_span(
                "llm_node",
                attributes={"lk.response.text": SECRET, "lk.room": "secret"},
            ):
                pass

    def emit_clean() -> None:
        with clean_handle.session_scope():
            _emit_livekit_span(provider, room="clean", span_id_box=[])

    barrier = threading.Barrier(2)

    def wrap(fn):
        def inner():
            barrier.wait()
            fn()

        return inner

    threads = [
        threading.Thread(target=wrap(emit_secret)),
        threading.Thread(target=wrap(emit_clean)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    clean_bundle = clean_adapter.recorder.close()
    secret_adapter.recorder.close()
    assert SECRET not in clean_bundle.model_dump_json()


def test_provider_adapter_close_releases_replay_state() -> None:
    """A reused provider adapter releases its replay/dedupe maps on close()."""

    from earshot.adapters.providers.base import AdapterUpdate, ProviderAdapter

    adapter = ProviderAdapter("test-provider")

    def factory(update_id: str) -> AdapterUpdate:
        return AdapterUpdate(
            provider="test-provider",
            event_type="event",
            update_id=update_id,
            correlation_id="corr",
            _apply_update=lambda turn: None,
        )

    adapter._remember({"seq": 1}, factory, native_update_id="native-1")
    assert len(adapter._updates) == 1
    assert len(adapter._native_updates) == 1

    adapter.close()
    assert len(adapter._updates) == 0
    assert len(adapter._native_updates) == 0


def test_pipecat_concurrent_sessions_do_not_cross_contaminate() -> None:
    """Pipecat parity for the concurrency isolation guarantee."""

    provider = TracerProvider()
    n = 12
    adapters = [PipecatAdapter(_recorder()) for _ in range(n)]
    handles = [a.attach(provider) for a in adapters]
    span_ids: list[int] = [0] * n
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        barrier.wait()
        with handles[i].session_scope():
            tracer = provider.get_tracer("pipecat")
            with tracer.start_as_current_span(
                "llm",
                attributes={"conversation.id": f"conv-{i}", "turn.number": i},
            ) as span:
                span_ids[i] = span.get_span_context().span_id

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i, adapter in enumerate(adapters):
        bundle = adapter.recorder.close()
        own = format(span_ids[i], "016x")
        assert _span_ids(bundle) == {own}, f"pipecat session {i} contaminated"


def test_pipecat_reports_unattributed_span_loss_without_content() -> None:
    """Pipecat exposes the same content-free routing health as LiveKit."""

    provider = TracerProvider()
    first = PipecatAdapter(_recorder())
    second = PipecatAdapter(_recorder())
    first_handle = first.attach(provider)
    second_handle = second.attach(provider)

    tracer = provider.get_tracer("pipecat")
    with tracer.start_as_current_span(
        "llm",
        attributes={"conversation.id": "orphan", "private.value": SECRET},
    ):
        pass

    assert first_handle.status.quarantined_span_count == 1
    assert second_handle.status.quarantined_span_count == 1
    for adapter in (first, second):
        bundle = adapter.recorder.close()
        assert [
            (item.signal, item.availability, item.reason)
            for item in bundle.profile.coverage
            if item.signal == "pipecat.span.routing"
        ] == [
            (
                "pipecat.span.routing",
                "partial",
                "unattributed_span_quarantined",
            )
        ]
        assert SECRET not in bundle.model_dump_json()
