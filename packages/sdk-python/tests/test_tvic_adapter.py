from __future__ import annotations

from collections.abc import Iterator, Mapping
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
from earshot.contract import UINT64_MAX, IncidentBundle
from earshot.validation import validate_incident

START = 1_752_800_000_000_000_000


class _ObservedMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.accesses = 0

    def __getitem__(self, key: str) -> object:
        self.accesses += 1
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        self.accesses += 1
        return iter(())

    def __len__(self) -> int:
        self.accesses += 1
        return 0


def _observation(
    sequence: int,
    name: str,
    *,
    at_ms: int | float,
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
        "factId": fact_id or f"{epoch}-fact-{sequence}",
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
                "first_audio_ms": 350,
                "first_token_ms": 140,
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
    assert not any(
        measurement.name == "earshot.llm.ttft"
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    )
    first_token = next(
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.response.first_token"
    )
    first_audio = next(
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.response.first_audio_generated"
    )
    assert first_token.participant_id == "participant-agent"
    assert first_token.time.monotonic_time_nano == "140000000"
    assert first_audio.time.monotonic_time_nano == "350000000"
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
    assert session_start.attributes["earshot.integration.tvic.fact_id"] == "runtime-epoch-a-fact-1"
    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=1,
    )
    metrics = analysis.projections.turns[0].metrics
    assert metrics.first_token_latency.value == pytest.approx(140)
    assert metrics.generated_response_latency.value == pytest.approx(350)


def test_estimated_session_end_time_is_not_presented_as_measured() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "turn.end",
                at_ms=750,
                turn_id="turn-1",
                attributes={
                    "at_ms_estimated": True,
                    "status": "completed",
                    "terminal_persisted": True,
                    "total_ms": 600,
                },
            ),
            _observation(
                2,
                "session.end",
                at_ms=750,
                attributes={
                    "at_ms_estimated": True,
                    "sequence_discontinuity": True,
                    "status": "completed",
                },
            ),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    turn_end = next(
        event
        for event in bundle.profile.events
        if event.attributes.get("earshot.integration.tvic.sequence") == 1
    )
    assert turn_end.evidence is not None
    assert turn_end.evidence.confidence == "estimated"
    assert turn_end.time.monotonic_time_nano == "750000000"
    assert turn_end.time.uncertainty_nano is None
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.session.end.time"
        and coverage.reason == "tvic_runtime_session_end_time_estimated"
        for coverage in bundle.profile.coverage
    )


@pytest.mark.parametrize("cause", ("timeout", "explicit"))
def test_non_user_interruption_causes_are_not_attributed_to_the_user(cause: str) -> None:
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
                at_ms=5,
                turn_id="turn-1",
                attributes={"cause": cause},
            ),
            _observation(
                4,
                "turn.end",
                at_ms=10,
                turn_id="turn-1",
                attributes={"status": "cancelled", "cancel_reason": cause},
            ),
            _observation(5, "session.end", at_ms=10, attributes={"status": "cancelled"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    interruption = next(
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.interruption.accepted"
    )
    assert interruption.participant_id is None
    assert interruption.attributes["earshot.integration.tvic.cause"] == cause


def test_missing_interruption_fact_is_coverage_not_a_synthetic_late_event() -> None:
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
                at_ms=100,
                turn_id="turn-1",
                attributes={"status": "cancelled", "cancel_reason": "barge_in"},
            ),
            _observation(4, "session.end", at_ms=100, attributes={"status": "cancelled"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert not any(
        event.event_name == "earshot.interruption.accepted" for event in bundle.profile.events
    )
    interruption_coverage = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.turn.interruption"
    )
    assert interruption_coverage.availability == "not_observed"
    assert bundle.profile.manifest.completeness == "incomplete"


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


def test_restart_keeps_session_relative_time_without_adding_recovery_gap() -> None:
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
            epoch="runtime-epoch-a",
        )
    )
    adapter.consume(
        _observation(2, "turn.start", at_ms=5_000.5, turn_id="turn-a", epoch="runtime-epoch-a")
    )
    adapter.consume(
        _observation(
            1,
            "session.resume",
            at_ms=10_000,
            epoch="runtime-epoch-b",
            attributes={"recovery_gap_ms": 5_000, "session_elapsed_ms": 10_000},
        )
    )
    adapter.consume(
        _observation(2, "turn.start", at_ms=10_000, turn_id="turn-b", epoch="runtime-epoch-b")
    )
    adapter.consume(
        _observation(
            3,
            "turn.end",
            at_ms=10_010,
            turn_id="turn-b",
            epoch="runtime-epoch-b",
            attributes={"status": "completed", "first_token_ms": 3},
        )
    )
    adapter.consume(
        _observation(
            4,
            "session.end",
            at_ms=10_010,
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
    assert not any(coverage.signal == "tvic.runtime.epoch" for coverage in bundle.profile.coverage)
    resume_event = next(
        event
        for event in bundle.profile.events
        if event.attributes.get("earshot.integration.tvic.epoch") == "runtime-epoch-b"
        and event.attributes.get("earshot.integration.tvic.sequence") == 1
    )
    assert resume_event.time.monotonic_time_nano == "10000000000"
    assert resume_event.time.uncertainty_nano == "1000000"
    assert resume_event.evidence is not None
    assert resume_event.evidence.confidence == "measured"
    monotonic_points = [
        int(event.time.monotonic_time_nano or "0") for event in bundle.profile.events
    ]
    assert monotonic_points == sorted(monotonic_points)


def test_sequence_gap_is_recorded_as_unattributed_observation_loss() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(2, "turn.start", at_ms=1, turn_id="turn-1"),
            _observation(
                4,
                "turn.end",
                at_ms=10,
                turn_id="turn-1",
                attributes={"status": "completed"},
            ),
            _observation(5, "session.end", at_ms=10, attributes={"status": "completed"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    gap = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.observation.sequence_gap"
    )
    assert gap.dropped_count == 1
    assert bundle.profile.manifest.completeness == "incomplete"


def test_initial_sequence_gap_is_recorded_when_ingestion_starts_late() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(2, "turn.start", at_ms=10, turn_id="turn-1"),
            _observation(
                3,
                "turn.end",
                at_ms=20,
                turn_id="turn-1",
                attributes={"status": "completed"},
            ),
            _observation(4, "session.end", at_ms=20, attributes={"status": "completed"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    gap = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.observation.sequence_gap"
    )
    assert gap.dropped_count == 1
    assert bundle.profile.manifest.completeness == "incomplete"


def test_repeated_drop_facts_accumulate_their_count_in_coverage() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(
                2,
                "observation.dropped",
                at_ms=10,
                attributes={"drop_reason": "queue_overflow", "dropped_count": 3},
            ),
            _observation(
                3,
                "observation.dropped",
                at_ms=20,
                attributes={"drop_reason": "queue_overflow", "dropped_count": 50},
            ),
            _observation(4, "session.end", at_ms=20, attributes={"status": "completed"}),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    queue_coverage = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.observation.queue_overflow"
    )
    assert queue_coverage.dropped_count == 53
    assert bundle.profile.manifest.completeness == "incomplete"


def test_turn_state_and_interruption_identity_survive_an_epoch_change() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
                epoch="runtime-epoch-a",
            ),
            _observation(2, "turn.start", at_ms=0, turn_id="turn-1", epoch="runtime-epoch-a"),
            _observation(
                3,
                "turn.interruption",
                at_ms=10,
                turn_id="turn-1",
                epoch="runtime-epoch-a",
                attributes={"cause": "barge_in"},
            ),
            _observation(
                1,
                "session.resume",
                at_ms=10,
                epoch="runtime-epoch-b",
                attributes={"recovery_gap_ms": 0, "session_elapsed_ms": 10},
            ),
            _observation(
                2,
                "turn.end",
                at_ms=20,
                turn_id="turn-1",
                epoch="runtime-epoch-b",
                attributes={"status": "cancelled", "cancel_reason": "barge_in"},
            ),
            _observation(
                3,
                "session.end",
                at_ms=20,
                epoch="runtime-epoch-b",
                attributes={"status": "completed"},
            ),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert (
        sum(event.event_name == "earshot.interruption.accepted" for event in bundle.profile.events)
        == 1
    )
    assert (
        sum(
            event.turn_id == "turn-1"
            and event.event_name in {"earshot.turn.committed", "framework.event"}
            for event in bundle.profile.events
        )
        == 2
    )
    assert not any(coverage.signal == "tvic.turn.end" for coverage in bundle.profile.coverage)


def test_logical_turn_fact_id_cannot_be_replayed_as_a_second_epoch_fact() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    adapter.consume(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
            epoch="runtime-epoch-a",
        )
    )
    adapter.consume(
        _observation(2, "turn.start", at_ms=0, turn_id="turn-1", epoch="runtime-epoch-a")
    )
    adapter.consume(
        _observation(
            3,
            "turn.end",
            at_ms=10,
            fact_id="stable-turn-end",
            turn_id="turn-1",
            epoch="runtime-epoch-a",
            attributes={"status": "completed"},
        )
    )
    adapter.consume(
        _observation(
            1,
            "session.resume",
            at_ms=0,
            epoch="runtime-epoch-b",
            attributes={"recovery_gap_ms": 0, "session_elapsed_ms": 10},
        )
    )

    with pytest.raises(TVICObservationError, match="reused"):
        adapter.consume(
            _observation(
                2,
                "turn.end",
                at_ms=20,
                fact_id="stable-turn-end",
                turn_id="turn-1",
                epoch="runtime-epoch-b",
                attributes={"status": "completed"},
            )
        )
    adapter.consume(
        _observation(
            3,
            "session.end",
            at_ms=20,
            epoch="runtime-epoch-b",
            attributes={"status": "completed"},
        )
    )
    bundle = adapter.close()

    assert sum(event.turn_id == "turn-1" for event in bundle.profile.events) == 2


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


@pytest.mark.parametrize("as_float", [False, True])
def test_maximum_millisecond_timestamp_preserves_uint64_range(as_float: bool) -> None:
    at_ms = UINT64_MAX // 1_000_000
    observation_at_ms = float(at_ms) if as_float else at_ms
    started_at_unix_nano = UINT64_MAX - at_ms * 1_000_000 - 615
    adapter = TVICRuntimeAdapter.create(
        session_id="tvic-call",
        started_at_unix_nano=started_at_unix_nano,
    )

    adapter.consume(_observation(1, "session.start", at_ms=observation_at_ms))
    bundle = adapter.close()

    event = next(event for event in bundle.profile.events if event.evidence is not None)
    expected_time = started_at_unix_nano + at_ms * 1_000_000
    assert event.time.source_time_unix_nano == str(expected_time)
    assert event.time.monotonic_time_nano == str(at_ms * 1_000_000)


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


def test_sink_rejects_mutated_model_mapping_before_inspecting_it() -> None:
    observation = TVICObservation.model_validate(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )
    attributes = _ObservedMapping()
    object.__setattr__(observation, "attributes", attributes)
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)

    sink.enqueue(observation)

    assert attributes.accesses == 0
    assert sink.error is not None
    assert sink.dropped_count == 1


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
    original_record_event = adapter.recorder.record_event

    def fail_record_turn_event(*args: object, **kwargs: object) -> object:
        if kwargs.get("turn_id") == "turn-1":
            raise RuntimeError("event journal unavailable")
        return original_record_event(*args, **kwargs)

    adapter.recorder.record_event = fail_record_turn_event  # type: ignore[method-assign]
    with pytest.raises(TVICObservationError, match="RuntimeError"):
        adapter.consume(
            _observation(
                3,
                "turn.end",
                at_ms=30,
                turn_id="turn-1",
                attributes={"status": "completed", "first_token_ms": 10, "total_ms": 30},
            )
        )
    adapter.recorder.record_event = original_record_event  # type: ignore[method-assign]

    bundle = adapter.close()

    assert not any(
        event.turn_id == "turn-1" and event.event_name == "framework.event"
        for event in bundle.profile.events
    )
    assert any(
        coverage.signal == "tvic.turn.end" and coverage.availability == "partial"
        for coverage in bundle.profile.coverage
    )


def test_repeated_dispatch_failures_accumulate_ingress_loss_count() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    adapter.consume(
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )
    original_record_event = adapter.recorder.record_event

    def fail_turn_terminal_event(
        event_name: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if event_name == "framework.event" and kwargs.get("turn_id") is not None:
            raise RuntimeError("turn event journal unavailable")
        return original_record_event(event_name, *args, **kwargs)

    adapter.recorder.record_event = fail_turn_terminal_event  # type: ignore[method-assign]
    for sequence, turn_id in ((2, "turn-1"), (3, "turn-2")):
        with pytest.raises(TVICObservationError, match="RuntimeError"):
            adapter.consume(
                _observation(
                    sequence,
                    "turn.end",
                    at_ms=sequence * 10,
                    turn_id=turn_id,
                    attributes={"status": "completed"},
                )
            )
    adapter.recorder.record_event = original_record_event  # type: ignore[method-assign]
    adapter.consume(_observation(4, "session.end", at_ms=30, attributes={"status": "completed"}))

    bundle = adapter.close()

    ingress_failure = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.ingress.failure"
    )
    sequence_gap = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.observation.sequence_gap"
    )
    assert ingress_failure.dropped_count == 2
    assert sequence_gap.dropped_count == 2
    assert bundle.profile.manifest.completeness == "incomplete"


def test_partial_turn_end_dispatch_marks_incident_incomplete() -> None:
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
    original_record_event = adapter.recorder.record_event
    turn_end_event_calls = 0

    def fail_after_one_turn_end_event(*args: object, **kwargs: object) -> object:
        nonlocal turn_end_event_calls
        if kwargs.get("turn_id") == "turn-1":
            turn_end_event_calls += 1
            if turn_end_event_calls == 2:
                raise RuntimeError("second turn-end event write failed")
        return original_record_event(*args, **kwargs)

    adapter.recorder.record_event = fail_after_one_turn_end_event  # type: ignore[method-assign]
    with pytest.raises(TVICObservationError, match="RuntimeError"):
        adapter.consume(
            _observation(
                3,
                "turn.end",
                at_ms=30,
                turn_id="turn-1",
                attributes={
                    "status": "completed",
                    "endpoint_ms": 2,
                    "first_token_ms": 10,
                    "first_audio_ms": 20,
                    "total_ms": 30,
                },
            )
        )
    adapter.recorder.record_event = original_record_event  # type: ignore[method-assign]
    adapter.consume(_observation(4, "session.end", at_ms=30, attributes={"status": "completed"}))

    bundle = adapter.close()

    assert (
        sum(event.event_name == "earshot.response.first_token" for event in bundle.profile.events)
        == 1
    )
    assert not any(
        event.event_name == "earshot.response.first_audio_generated"
        for event in bundle.profile.events
    )
    assert bundle.profile.manifest.completeness == "incomplete"


def test_session_end_with_open_turn_marks_final_incident_incomplete() -> None:
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
    adapter.consume(_observation(3, "session.end", at_ms=50, attributes={"status": "completed"}))

    bundle = adapter.close()

    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.turn.end"
        and coverage.reason == "tvic_runtime_turn_end_not_observed"
        for coverage in bundle.profile.coverage
    )


def test_turn_end_without_persistence_status_marks_incident_incomplete() -> None:
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
    adapter.consume(
        _observation(
            3,
            "turn.end",
            at_ms=30,
            turn_id="turn-1",
            attributes={"status": "completed"},
        )
    )
    adapter.consume(_observation(4, "session.end", at_ms=30, attributes={"status": "completed"}))

    bundle = adapter.close()

    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.turn.terminal_persistence"
        and coverage.reason == "tvic_runtime_terminal_persistence_unknown"
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
    assert sink.error is None
    assert sink.drain() == 0
    assert sink.dropped_count == 1
    bundle = sink.close()

    assert sink.error is not None
    assert any(coverage.signal == "tvic.ingress.failure" for coverage in bundle.profile.coverage)


def test_sink_does_not_double_count_a_recorder_dispatch_failure() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    for observation in (
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        ),
        _observation(
            2,
            "turn.end",
            at_ms=10,
            turn_id="turn-1",
            attributes={"status": "completed"},
        ),
        _observation(3, "session.end", at_ms=10, attributes={"status": "completed"}),
    ):
        sink.enqueue(observation)
    original_record_event = adapter.recorder.record_event

    def fail_turn_event(event_name: str, *args: object, **kwargs: object) -> object:
        if event_name == "framework.event" and kwargs.get("turn_id") == "turn-1":
            raise RuntimeError("event journal unavailable")
        return original_record_event(event_name, *args, **kwargs)

    adapter.recorder.record_event = fail_turn_event  # type: ignore[method-assign]

    assert sink.drain() == 2
    bundle = sink.close()

    failure = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.ingress.failure"
    )
    assert sink.dropped_count == 1
    assert failure.dropped_count == 1


def test_sink_snapshots_mapping_before_deferred_validation() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    observation = _observation(
        1,
        "session.start",
        at_ms=0,
        attributes={"agent_id": "agent-a", "channel": "simulated"},
    )
    sink.enqueue(observation)
    attributes = observation["attributes"]
    assert isinstance(attributes, dict)
    attributes["channel"] = "unsupported-after-enqueue"

    assert sink.error is None
    assert sink.drain() == 1
    assert adapter.accepted_observations == 1
    assert sink.error is None


def test_sink_rejects_custom_mappings_without_executing_them() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    custom_envelope = _ObservedMapping()
    custom_attributes = _ObservedMapping()
    envelope_with_custom_attributes = _observation(1, "session.start", at_ms=0)
    envelope_with_custom_attributes["attributes"] = custom_attributes

    sink.enqueue(custom_envelope)  # type: ignore[arg-type]
    sink.enqueue(envelope_with_custom_attributes)

    assert custom_envelope.accesses == 0
    assert custom_attributes.accesses == 0
    assert sink.error is not None
    assert sink.dropped_count == 2


def test_sink_keeps_session_facts_after_an_unroutable_runtime_fact() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    sink.enqueue(
        _observation(
            1,
            "observation.dropped",
            at_ms=1,
            attributes={
                "drop_reason": "pending_session_limit",
                "dropped_count": 3,
                "queue_capacity": 256,
            },
            session_id="runtime-observation",
        )
    )
    for observation in _complete_observations():
        sink.enqueue(observation)

    assert sink.drain() == len(_complete_observations())
    bundle = sink.close()

    assert sink.dropped_count == 0
    assert sink.error is not None
    assert adapter.accepted_observations == len(_complete_observations())
    assert bundle.profile.manifest.completeness == "complete"
    assert not any(
        coverage.signal in {"tvic.ingress.queue", "tvic.ingress.failure"}
        for coverage in bundle.profile.coverage
    )


def test_sink_skips_one_stale_fact_without_losing_the_healthy_session_tail() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter, max_queue=8)
    observations = [
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
            epoch="runtime-epoch-a",
        ),
        _observation(
            1,
            "session.resume",
            at_ms=100,
            epoch="runtime-epoch-b",
            attributes={"recovery_gap_ms": 100, "session_elapsed_ms": 100},
        ),
        _observation(
            2,
            "turn.start",
            at_ms=1,
            turn_id="late-old-turn",
            epoch="runtime-epoch-a",
        ),
        _observation(2, "turn.start", at_ms=110, turn_id="turn-1", epoch="runtime-epoch-b"),
        _observation(
            3,
            "turn.end",
            at_ms=120,
            turn_id="turn-1",
            epoch="runtime-epoch-b",
            attributes={"status": "completed"},
        ),
        _observation(
            4,
            "session.end",
            at_ms=120,
            epoch="runtime-epoch-b",
            attributes={"status": "completed"},
        ),
    ]
    for observation in observations:
        sink.enqueue(observation)

    assert sink.drain() == 5
    bundle = sink.close()

    assert sink.dropped_count == 1
    assert bundle.profile.session.status == "completed"
    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        event.attributes.get("earshot.integration.tvic.fact_id") == "runtime-epoch-b-fact-4"
        for event in bundle.profile.events
    )
    assert not any(coverage.signal == "tvic.ingress.queue" for coverage in bundle.profile.coverage)
    assert any(
        coverage.signal == "tvic.ingress.failure" and coverage.dropped_count == 1
        for coverage in bundle.profile.coverage
    )


def test_sink_counts_an_invalid_epoch_as_admission_loss_not_queue_overflow() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)
    sink.enqueue(
        _observation(
            1,
            "session.start",
            at_ms=0,
            epoch="e" * 257,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    )

    bundle = sink.close()

    assert sink.dropped_count == 1
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.ingress.failure" and coverage.dropped_count == 1
        for coverage in bundle.profile.coverage
    )
    assert not any(coverage.signal == "tvic.ingress.queue" for coverage in bundle.profile.coverage)


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
    for observation in _complete_observations():
        sink.enqueue(observation)
    bundle = sink.close()
    before = sink.dropped_count

    sink.enqueue(_complete_observations()[0])
    repeated_bundle = sink.close()

    assert sink.dropped_count == before + 1
    assert bundle.profile.manifest.completeness == "complete"
    assert repeated_bundle.profile.manifest.completeness == "complete"


def test_sink_close_bundle_copy_does_not_block_runtime_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    sink = TVICRuntimeObservationSink(adapter)

    copy_entered = Event()
    allow_copy = Event()
    original_model_copy = IncidentBundle.model_copy

    def blocked_model_copy(
        bundle: IncidentBundle, *args: object, **kwargs: object
    ) -> IncidentBundle:
        copy_entered.set()
        if not allow_copy.wait(timeout=2):
            raise TimeoutError("test did not release the bundle copy")
        return original_model_copy(bundle, *args, **kwargs)

    monkeypatch.setattr(IncidentBundle, "model_copy", blocked_model_copy)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            for sequence in (1, 2):
                copy_entered.clear()
                allow_copy.clear()
                close_result = executor.submit(sink.close)
                assert copy_entered.wait(timeout=1)
                enqueue_result = executor.submit(
                    sink.enqueue, _observation(sequence, "session.resume", at_ms=sequence)
                )
                enqueue_result.result(timeout=1)
                allow_copy.set()
                close_result.result(timeout=1)
    finally:
        allow_copy.set()


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
                fact_id="provisional-turn-end",
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
            _observation(
                4,
                "turn.end",
                at_ms=50,
                fact_id="persisted-turn-end",
                turn_id="turn-1",
                attributes={
                    "status": "failed",
                    "audio_delivered": False,
                    "audio_error_code": "provider_timeout",
                    "text_delivered": False,
                    "terminal_persisted": True,
                    "error_code": "provider_timeout",
                    "error_category": "timeout",
                },
            ),
            _observation(5, "session.end", at_ms=50, attributes={"status": "failed"}),
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
    assert (
        sum(
            event.event_name == "framework.event" and event.turn_id == "turn-1"
            for event in bundle.profile.events
        )
        == 1
    )
    error_event = next(
        event for event in bundle.profile.events if event.event_name == "framework.error"
    )
    assert error_event.attributes["earshot.integration.tvic.error_code"] == "provider_timeout"


def test_provisional_turn_end_stays_open_until_the_persisted_terminal_fact() -> None:
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
                at_ms=30,
                fact_id="provisional-turn-end",
                turn_id="turn-1",
                attributes={"status": "completed", "terminal_persisted": False},
            ),
            _observation(
                4,
                "turn.end",
                at_ms=30,
                fact_id="persisted-turn-end",
                turn_id="turn-1",
                attributes={"status": "completed", "terminal_persisted": True},
            ),
            _observation(
                5,
                "session.end",
                at_ms=30,
                attributes={"status": "completed", "terminal_source": "normal_completion"},
            ),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert bundle.profile.manifest.completeness == "complete"
    assert (
        sum(
            event.event_name == "framework.event" and event.turn_id == "turn-1"
            for event in bundle.profile.events
        )
        == 1
    )
    assert any(
        coverage.signal == "tvic.turn.terminal_persistence" and coverage.availability == "partial"
        for coverage in bundle.profile.coverage
    )


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


@pytest.mark.parametrize(
    ("discontinuity_at", "expected_completeness"),
    [(None, "complete"), ("session.resume", "incomplete"), ("session.end", "incomplete")],
)
def test_observed_cancellations_are_outcomes_only_complete_with_continuous_timeline(
    discontinuity_at: str | None,
    expected_completeness: str,
) -> None:
    session_end_attributes: dict[str, str | bool] = {
        "status": "cancelled",
        "cancel_reason": "caller_hangup",
        "terminal_source": "caller_abort",
    }
    if discontinuity_at == "session.end":
        session_end_attributes["sequence_discontinuity"] = True
    observations = [
        _observation(
            1,
            "session.start",
            at_ms=0,
            attributes={"agent_id": "agent-a", "channel": "simulated"},
        )
    ]
    next_sequence = 2
    if discontinuity_at == "session.resume":
        observations.append(
            _observation(
                next_sequence,
                "session.resume",
                at_ms=10,
                attributes={
                    "recovery_gap_ms": 10,
                    "sequence_discontinuity": True,
                    "session_elapsed_ms": 10,
                },
            )
        )
        next_sequence += 1
    observations.extend(
        [
            _observation(next_sequence, "turn.start", at_ms=10, turn_id="turn-1"),
            _observation(
                next_sequence + 1,
                "turn.end",
                at_ms=50,
                turn_id="turn-1",
                attributes={
                    "status": "cancelled",
                    "cancel_reason": "transport_lost",
                    "audio_delivered": False,
                    "text_delivered": False,
                    "terminal_persisted": True,
                },
            ),
            _observation(
                next_sequence + 2,
                "session.end",
                at_ms=50,
                attributes=session_end_attributes,
            ),
        ]
    )
    bundle = ingest_tvic_observations(
        observations,
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert bundle.profile.session.status == "cancelled"
    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == expected_completeness
    assert not any(
        coverage.signal in {"tvic.session.result", "tvic.turn.result", "tvic.output.audio"}
        for coverage in bundle.profile.coverage
    )
    turn_end = next(
        event
        for event in bundle.profile.events
        if event.turn_id == "turn-1"
        and event.attributes.get("earshot.integration.tvic.status") == "cancelled"
    )
    assert turn_end.attributes["earshot.integration.tvic.audio_delivered"] is False


def test_caller_hangup_with_unmatched_turn_end_is_incomplete_without_latency_fields() -> None:
    bundle = ingest_tvic_observations(
        [
            _observation(
                1,
                "session.start",
                at_ms=0,
                attributes={"agent_id": "agent-a", "channel": "simulated"},
            ),
            _observation(
                2, "turn.end", at_ms=30, turn_id="turn-1", attributes={"status": "completed"}
            ),
            _observation(
                3,
                "session.end",
                at_ms=30,
                attributes={
                    "status": "cancelled",
                    "cancel_reason": "caller_hangup",
                    "terminal_source": "caller_abort",
                },
            ),
        ],
        session_id="tvic-call",
        started_at_unix_nano=START,
    )

    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(
        coverage.signal == "tvic.turn.start" and coverage.availability == "not_observed"
        for coverage in bundle.profile.coverage
    )


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
    failure_coverage = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.ingress.failure"
    )
    assert queue_coverage.dropped_count == 1
    assert failure_coverage.dropped_count == 1
    assert sink.dropped_count == 2


def test_sink_marks_zero_to_one_drop_during_finalization_incomplete() -> None:
    adapter = TVICRuntimeAdapter.create(session_id="tvic-call", started_at_unix_nano=START)
    original_close = adapter.close
    entered = Event()
    release = Event()

    def blocked_close() -> IncidentBundle:
        entered.set()
        release.wait(timeout=1)
        return original_close()

    adapter.close = blocked_close  # type: ignore[method-assign]
    sink = TVICRuntimeObservationSink(adapter)
    for observation in _complete_observations():
        sink.enqueue(observation)

    with ThreadPoolExecutor(max_workers=1) as executor:
        close_future = executor.submit(sink.close)
        try:
            assert entered.wait(timeout=1)
            sink.enqueue(_observation(5, "session.resume", at_ms=500))
        finally:
            release.set()
        bundle = close_future.result(timeout=1)

    failure_coverage = next(
        coverage
        for coverage in bundle.profile.coverage
        if coverage.signal == "tvic.ingress.failure"
    )
    assert failure_coverage.dropped_count == sink.dropped_count == 1
    assert bundle.profile.manifest.completeness == "incomplete"
