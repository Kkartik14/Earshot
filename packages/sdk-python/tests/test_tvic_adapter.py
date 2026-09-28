from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter, sleep

import pytest

from earshot.adapters.tvic import (
    TVICObservation,
    TVICObservationError,
    TVICRuntimeAdapter,
    TVICRuntimeObservationSink,
    ingest_tvic_observations,
)
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.contract import UINT64_MAX
from earshot.validation import validate_incident

START = 1_752_800_000_000_000_000


def _observation(
    sequence: int,
    name: str,
    *,
    at_ms: int,
    fact_id: str | None = None,
    turn_id: str | None = None,
    attributes: dict[str, object] | None = None,
    epoch: str = "runtime-epoch-a",
    session_id: str = "tvic-call",
) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "epoch": epoch,
        "sequence": sequence,
        "factId": fact_id or f"fact-{sequence}",
        "name": name,
        "sessionId": session_id,
        "atMs": at_ms,
    }
    if turn_id is not None:
        value["turnId"] = turn_id
    if attributes is not None:
        value["attributes"] = attributes
    return value


def _complete_observations() -> list[dict[str, object]]:
    return [
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        ),
        _observation(2, "turn.start", at_ms=0, turn_id="turn-1", attributes={"turn_sequence": 1}),
        _observation(
            3,
            "turn.end",
            at_ms=500,
            turn_id="turn-1",
            attributes={
                "first_audio_ms": 140,
                "first_token_ms": 350,
                "status": "completed",
                "terminal_persisted": True,
                "total_ms": 500,
            },
        ),
        _observation(
            4,
            "session.end",
            at_ms=500,
            attributes={"status": "completed", "terminal_source": "normal_completion"},
        ),
    ]


def test_tvic_runtime_facts_project_to_analyzable_earshot_evidence() -> None:
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=START,
    )
    assert adapter.consume_many(_complete_observations()) == 4
    bundle = adapter.close()

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    assert bundle.profile.session.status == "completed"
    assert any(
        sample.measurements[0].name == "tvic.turn.first_audio_latency"
        for sample in bundle.profile.quality_samples
    )
    assert any(
        sample.measurements[0].name == "earshot.llm.ttft"
        for sample in bundle.profile.quality_samples
    )
    assert any(
        event.attributes.get("earshot.integration.tvic.sequence") == 2
        for event in bundle.profile.events
    )
    session_start = next(
        event
        for event in bundle.profile.events
        if event.attributes.get("earshot.integration.tvic.sequence") == 1
    )
    assert session_start.attributes["earshot.integration.tvic.epoch"] == "runtime-epoch-a"
    assert session_start.attributes["earshot.integration.tvic.fact_id"] == "fact-1"
    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=1,
    )
    metrics = analysis.projections.turns[0].metrics
    assert metrics.first_token_latency.value == pytest.approx(350)
    assert metrics.generated_response_latency.value is None


def test_tvic_replay_is_idempotent_but_fact_conflicts_and_sequence_regressions_fail() -> None:
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=START,
    )
    first = _complete_observations()[0]
    assert adapter.consume(first)
    assert not adapter.consume(first)
    with pytest.raises(TVICObservationError, match="reused"):
        adapter.consume({**first, "atMs": 1, "sequence": 2})
    with pytest.raises(TVICObservationError, match="regressed"):
        adapter.consume(
            _observation(
                1,
                "session.resume",
                at_ms=1,
                fact_id="fact-resume",
                attributes={"recovery_gap_ms": 1, "session_elapsed_ms": 1},
            )
        )


def test_tvic_input_rejects_transcript_audio_and_unknown_metadata() -> None:
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=START,
    )
    for field in ("transcript", "audio", "customer_phone"):
        with pytest.raises(TVICObservationError, match="validation failed"):
            adapter.consume(
                _observation(
                    1,
                    "session.start",
                    at_ms=0,
                    attributes={field: "must not cross the seam"},
                )
            )


def test_runtime_wide_drop_is_rejected_and_missing_close_is_provisional() -> None:
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=START,
    )
    adapter.consume(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )
    with pytest.raises(TVICObservationError, match="runtime-wide"):
        adapter.consume(
            {
                **_observation(
                    1,
                    "observation.dropped",
                    at_ms=20,
                    attributes={
                        "drop_reason": "queue_overflow",
                        "dropped_count": 2,
                        "queue_capacity": 1,
                    },
                ),
                "sessionId": "runtime-observation",
            }
        )
    bundle = adapter.close()

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    assert bundle.profile.manifest.finality == "provisional"
    assert not any(
        event.event_name == "earshot.response.first_audio_generated"
        for event in bundle.profile.events
    )


def test_epoch_scoped_identity_and_clock_domains_survive_runtime_restart() -> None:
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=START,
    )
    adapter.consume(
        _observation(
            1,
            "session.start",
            at_ms=100,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
            epoch="runtime-epoch-a",
        )
    )
    adapter.consume(
        _observation(
            1,
            "session.resume",
            at_ms=0,
            epoch="runtime-epoch-b",
            attributes={"recovery_gap_ms": 50, "session_elapsed_ms": 0},
        )
    )
    adapter.consume(
        _observation(2, "turn.start", at_ms=0, turn_id="turn-b", epoch="runtime-epoch-b")
    )
    adapter.consume(
        _observation(
            3,
            "turn.end",
            at_ms=10,
            turn_id="turn-b",
            epoch="runtime-epoch-b",
            attributes={"status": "completed", "first_token_ms": 3},
        )
    )
    adapter.consume(
        _observation(
            4,
            "session.end",
            at_ms=10,
            epoch="runtime-epoch-b",
            attributes={"status": "completed"},
        )
    )
    bundle = adapter.close()

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    event_ids = [event.event_id for event in bundle.profile.events]
    assert len(event_ids) == len(set(event_ids))
    observation_domains = {
        event.time.clock_domain_id for event in bundle.profile.events if event.evidence is not None
    }
    assert observation_domains == {bundle.profile.clock_domains[0].clock_domain_id}
    assert any(coverage.signal == "tvic.runtime.epoch" for coverage in bundle.profile.coverage)
    rebased_events = [
        event
        for event in bundle.profile.events
        if event.evidence is not None and event.time.uncertainty_nano == "51000000"
    ]
    assert rebased_events
    assert all(event.evidence.confidence == "estimated" for event in rebased_events)
    monotonic_points = [
        int(event.time.monotonic_time_nano or "0") for event in bundle.profile.events
    ]
    assert monotonic_points == sorted(monotonic_points)


def test_numeric_bounds_fail_before_recorder_admission() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    with pytest.raises(TVICObservationError, match="validation failed"):
        adapter.consume(_observation(1, "session.start", at_ms=10**20))
    with pytest.raises(TVICObservationError, match="validation failed"):
        adapter.consume(_observation(1 << 53, "session.start", at_ms=0))
    with pytest.raises(TVICObservationError, match="out of range"):
        adapter.consume(
            _observation(
                1,
                "observation.dropped",
                at_ms=0,
                attributes={"drop_reason": "queue_overflow", "dropped_count": 10**15},
            )
        )
    with pytest.raises(ValueError, match="uint64"):
        TVICRuntimeAdapter.create(
            session_id="tvic-call",
            started_at_unix_nano=UINT64_MAX + 1,
        )


def test_concurrent_replay_admission_is_idempotent() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    observation = _observation(
        1,
        "session.start",
        at_ms=0,
        attributes={"agent_id": "agent-a", "channel": "simulated"},
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(adapter.consume, [observation] * 32))

    assert sum(results) == 1
    assert adapter.accepted_observations == 1
    assert adapter.close().profile.manifest.finality == "provisional"


def test_mutated_model_is_revalidated_at_the_adapter_boundary() -> None:
    observation = TVICObservation.model_validate(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )
    with pytest.raises(TypeError, match="immutable"):
        observation.attributes["transcript"] = "must not cross the seam"
    dict.__setitem__(observation.attributes, "transcript", "must not cross the seam")
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)

    with pytest.raises(TVICObservationError, match="validation failed"):
        adapter.consume(observation)


def test_dispatch_failure_is_not_marked_as_a_successful_replay() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_record_event = adapter.recorder.record_event

    def fail_record_event(*args: object, **kwargs: object) -> object:
        raise RuntimeError("journal unavailable")

    adapter.recorder.record_event = fail_record_event  # type: ignore[method-assign]
    observation = _observation(
        1,
        "session.start",
        at_ms=0,
        attributes={"agent_id": "agent-a", "channel": "simulated"},
    )
    with pytest.raises(TVICObservationError, match="RuntimeError"):
        adapter.consume(observation)
    assert adapter.accepted_observations == 0
    adapter.recorder.record_event = original_record_event  # type: ignore[method-assign]
    with pytest.raises(TVICObservationError, match="previously failed"):
        adapter.consume(observation)


def test_dispatch_failure_diagnostic_is_retried_during_close() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_record_event = adapter.recorder.record_event
    original_record_coverage = adapter.recorder.record_coverage
    coverage_calls = 0

    def fail_record_event(*args: object, **kwargs: object) -> object:
        raise RuntimeError("journal unavailable")

    def fail_first_coverage(*args: object, **kwargs: object) -> object:
        nonlocal coverage_calls
        coverage_calls += 1
        if coverage_calls == 1:
            raise RuntimeError("coverage journal unavailable")
        return original_record_coverage(*args, **kwargs)

    adapter.recorder.record_event = fail_record_event  # type: ignore[method-assign]
    adapter.recorder.record_coverage = fail_first_coverage  # type: ignore[method-assign]
    with pytest.raises(TVICObservationError, match="RuntimeError"):
        adapter.consume(
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            )
        )

    adapter.recorder.record_event = original_record_event  # type: ignore[method-assign]
    bundle = adapter.close()

    assert any(
        coverage.signal == "tvic.ingress.dispatch.failure"
        and coverage.reason == "tvic_runtime_dispatch_failed"
        for coverage in bundle.profile.coverage
    )


def test_turn_end_failure_does_not_leave_terminal_event() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    adapter.consume(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )
    adapter.consume(_observation(2, "turn.start", at_ms=0, turn_id="turn-1"))
    original_record_quality_sample = adapter.recorder.record_quality_sample

    def fail_record_quality_sample(*args: object, **kwargs: object) -> object:
        raise RuntimeError("quality journal unavailable")

    adapter.recorder.record_quality_sample = fail_record_quality_sample  # type: ignore[method-assign]
    with pytest.raises(TVICObservationError, match="RuntimeError"):
        adapter.consume(
            _observation(
                3,
                "turn.end",
                at_ms=30,
                turn_id="turn-1",
                attributes={"status": "completed", "total_ms": 30},
            )
        )
    adapter.recorder.record_quality_sample = original_record_quality_sample  # type: ignore[method-assign]

    bundle = adapter.close()

    assert not any(
        event.turn_id == "turn-1" and event.event_name == "framework.event"
        for event in bundle.profile.events
    )
    assert any(
        coverage.signal == "tvic.turn.end" and coverage.availability == "partial"
        for coverage in bundle.profile.coverage
    )


def test_sink_filters_invalid_input_and_closes_with_failure_coverage() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    sink.enqueue(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"transcript": "must not cross the seam"},
        )
    )
    bundle = sink.close()

    assert sink.error is not None
    assert any(coverage.signal == "tvic.ingress.failure" for coverage in bundle.profile.coverage)


def test_sink_close_survives_ingress_diagnostic_failure() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    for observation in _complete_observations():
        sink.enqueue(observation)
    assert sink.drain() == 4
    original_record_coverage = adapter.recorder.record_coverage

    def fail_coverage(*args: object, **kwargs: object) -> object:
        raise RuntimeError("coverage journal unavailable")

    adapter.recorder.record_coverage = fail_coverage  # type: ignore[method-assign]
    sink.enqueue(_observation(5, "session.resume", at_ms=500))
    bundle = sink.close()
    adapter.recorder.record_coverage = original_record_coverage  # type: ignore[method-assign]

    assert sink.error is not None
    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.ingress.admission.failure"
        and coverage.reason == "tvic_runtime_adapter_admission_failed"
        for coverage in bundle.profile.coverage
    )


def test_sink_counts_enqueue_after_close_as_transport_loss() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    sink.close()
    before = sink.dropped_count

    sink.enqueue(_complete_observations()[0])

    assert sink.dropped_count == before + 1


@pytest.mark.parametrize("loss_kind", ["admission", "queue"])
def test_terminal_bundle_marks_ingress_loss_incomplete(loss_kind: str) -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    adapter.consume_many(_complete_observations())
    if loss_kind == "admission":
        adapter.record_ingress_failure()
    else:
        adapter.record_ingress_drop(1)

    bundle = adapter.close()

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == "incomplete"


def test_queue_diagnostic_failure_keeps_specific_provenance_after_generic_retry() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter, max_queue=8)
    for observation in _complete_observations():
        sink.enqueue(observation)
    assert sink.drain() == 4

    original_record_coverage = adapter.recorder.record_coverage
    coverage_calls = 0

    def fail_first_coverage(*args: object, **kwargs: object) -> object:
        nonlocal coverage_calls
        coverage_calls += 1
        if coverage_calls == 1:
            raise RuntimeError("queue coverage journal unavailable")
        return original_record_coverage(*args, **kwargs)

    adapter.recorder.record_coverage = fail_first_coverage  # type: ignore[method-assign]
    for sequence in range(5, 13):
        sink.enqueue(_observation(sequence, "session.resume", at_ms=sequence * 100))
    sink.enqueue(_observation(13, "session.resume", at_ms=1300))
    bundle = sink.close()
    adapter.recorder.record_coverage = original_record_coverage  # type: ignore[method-assign]

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.ingress.queue.failure"
        and coverage.reason == "tvic_runtime_adapter_queue_diagnostic_failed"
        for coverage in bundle.profile.coverage
    )


def test_negative_turn_outcomes_are_queryable_coverage() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(2, "turn.start", at_ms=0, turn_id="turn-1"),
            _observation(
                3,
                "turn.end",
                at_ms=50,
                turn_id="turn-1",
                attributes={
                    "status": "failed",
                    "audio_delivered": False,
                    "audio_error_code": "provider_timeout",
                    "text_delivered": False,
                    "terminal_persisted": False,
                    "error_code": "provider_timeout",
                    "error_category": "timeout",
                },
            ),
            _observation(4, "session.end", at_ms=50, attributes={"status": "failed"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    signals = {coverage.signal for coverage in bundle.profile.coverage}
    assert {
        "tvic.session.result",
        "tvic.turn.result",
        "tvic.output.audio",
        "tvic.output.audio.error",
        "tvic.output.text",
        "tvic.turn.terminal_persistence",
    } <= signals
    assert {
        coverage.reason
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.output.audio"
    } == {"tvic_runtime_audio_not_delivered"}
    assert {
        coverage.reason
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.output.audio.error"
    } == {"tvic_runtime_audio_error"}
    assert any(event.event_name == "framework.error" for event in bundle.profile.events)
    error_event = next(
        event for event in bundle.profile.events if event.event_name == "framework.error"
    )
    assert error_event.attributes["earshot.integration.tvic.error_code"] == "provider_timeout"


def test_explicit_interruption_is_not_duplicated_by_cancelled_turn_end() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(2, "turn.start", at_ms=0, turn_id="turn-1"),
            _observation(
                3,
                "turn.interruption",
                at_ms=20,
                turn_id="turn-1",
                attributes={"cause": "barge_in"},
            ),
            _observation(
                4,
                "turn.end",
                at_ms=30,
                turn_id="turn-1",
                attributes={"status": "cancelled", "cancel_reason": "barge_in"},
            ),
            _observation(5, "session.end", at_ms=30, attributes={"status": "cancelled"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert (
        sum(event.event_name == "earshot.interruption.accepted" for event in bundle.profile.events)
        == 1
    )
    assert not any(event.event_name == "framework.error" for event in bundle.profile.events)


def test_terminal_turn_status_and_interruption_cause_are_required_and_enum_bound() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    with pytest.raises(TVICObservationError, match="validation failed"):
        adapter.consume(_observation(1, "turn.end", at_ms=0, turn_id="turn-1"))
    with pytest.raises(TVICObservationError, match="validation failed"):
        adapter.consume(
            _observation(
                1,
                "turn.interruption",
                at_ms=0,
                turn_id="turn-1",
                attributes={"cause": "untrusted"},
            )
        )


def test_partial_close_records_open_turn_loss() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(2, "turn.start", at_ms=10, turn_id="turn-open"),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert any(
        coverage.signal == "tvic.turn.end" and coverage.availability == "partial"
        for coverage in bundle.profile.coverage
    )


def test_runtime_sink_enqueues_without_recorder_work_on_the_callback() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_consume = adapter.consume

    def slow_consume(value: object) -> bool:
        sleep(0.05)
        return original_consume(value)  # type: ignore[arg-type]

    adapter.consume = slow_consume  # type: ignore[method-assign]
    sink = TVICRuntimeObservationSink(adapter, max_queue=1)
    started = perf_counter()
    for observation in _complete_observations():
        sink.enqueue(observation)
    enqueue_seconds = perf_counter() - started
    bundle = sink.close(timeout=3)

    assert enqueue_seconds < 0.25
    assert sink.dropped_count >= 1
    assert any(coverage.signal == "tvic.ingress.queue" for coverage in bundle.profile.coverage)


def test_sink_close_timeout_covers_a_concurrent_drain() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_consume = adapter.consume
    entered = Event()
    release = Event()

    def blocked_consume(value: object) -> bool:
        entered.set()
        release.wait(timeout=1)
        return original_consume(value)  # type: ignore[arg-type]

    adapter.consume = blocked_consume  # type: ignore[method-assign]
    sink = TVICRuntimeObservationSink(adapter)
    sink.enqueue(_complete_observations()[0])
    with ThreadPoolExecutor(max_workers=1) as executor:
        drain_future = executor.submit(sink.drain)
        assert entered.wait(timeout=1)
        started = perf_counter()
        with pytest.raises(TimeoutError, match="did not quiesce"):
            sink.close(timeout=0.01)
        assert perf_counter() - started < 0.1
        release.set()
        assert drain_future.result(timeout=1) == 1

    bundle = sink.close(timeout=1)
    assert bundle.profile.manifest.finality == "provisional"


def test_sink_close_timeout_covers_its_own_slow_drain() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_consume = adapter.consume
    entered = Event()
    release = Event()

    def blocked_consume(value: object) -> bool:
        entered.set()
        release.wait(timeout=1)
        return original_consume(value)  # type: ignore[arg-type]

    adapter.consume = blocked_consume  # type: ignore[method-assign]
    sink = TVICRuntimeObservationSink(adapter)
    sink.enqueue(_complete_observations()[0])
    started = perf_counter()
    with pytest.raises(TimeoutError, match="did not quiesce"):
        sink.close(timeout=0.01)
    assert perf_counter() - started < 0.1

    release.set()
    bundle = sink.close(timeout=1)
    assert bundle.profile.manifest.finality == "provisional"


def test_sink_reconciles_drops_arriving_during_finalization() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_record_ingress_drop = adapter.record_ingress_drop
    entered = Event()
    release = Event()

    def blocked_record_ingress_drop(dropped_count: int) -> None:
        entered.set()
        release.wait(timeout=1)
        original_record_ingress_drop(dropped_count)

    adapter.record_ingress_drop = blocked_record_ingress_drop  # type: ignore[method-assign]
    sink = TVICRuntimeObservationSink(adapter, max_queue=1)
    sink.enqueue(_complete_observations()[0])
    sink.enqueue(_complete_observations()[1])
    with pytest.raises(TimeoutError):
        sink.close(timeout=0.01)
    assert entered.wait(timeout=1)
    sink.enqueue(_complete_observations()[2])
    release.set()
    bundle = sink.close(timeout=1)
    adapter.record_ingress_drop = original_record_ingress_drop  # type: ignore[method-assign]

    queue_coverage = next(
        coverage for coverage in bundle.profile.coverage if coverage.signal == "tvic.ingress.queue"
    )
    assert queue_coverage.dropped_count == sink.dropped_count == 2
