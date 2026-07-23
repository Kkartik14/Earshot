"""Concurrency + lifecycle isolation for framework span routing.

These tests exercise the real OpenTelemetry SDK provider path. Before the
process-scoped router they FAILED: one recorder-bound processor was added per
session to the shared provider, so every recorder ingested every session's
spans (cross-session contamination) and processors/recorders were retained
forever.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator

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
