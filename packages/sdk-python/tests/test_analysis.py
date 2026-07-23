from __future__ import annotations

from copy import deepcopy

import pytest

from earshot.analysis import analyze_incident, comparable_delta
from earshot.codec import analysis_input_sha256
from earshot.contract import (
    ClockDomain,
    Coverage,
    Event,
    Evidence,
    Operation,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
    ToolAnalysis,
)
from earshot.explanation import explain_incident
from earshot.validation import validate_derived_analysis, validate_incident
from incident_factory import point
from test_contract_validation import replace_profile

pytestmark = pytest.mark.unit


def analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000005000000000",
    )


def turn(analysis, index: int = 0) -> dict:
    return analysis.projections["turns"][index]


def metric(analysis, name: str) -> dict:
    return turn(analysis)["metrics"][name]


def test_same_domain_monotonic_delta_is_exact() -> None:
    delta = comparable_delta(point(1_000_000), point(3_500_000))
    assert delta.availability == "available"
    assert delta.nanoseconds == 2_500_000
    assert delta.basis == "monotonic"
    assert delta.confidence == "measured"


def test_zero_delta_is_observed_zero_not_missing() -> None:
    delta = comparable_delta(point(123), point(123))
    assert delta.availability == "available"
    assert delta.nanoseconds == 0


def test_uncertainty_marks_delta_estimated() -> None:
    delta = comparable_delta(point(1, uncertainty=3), point(5, uncertainty=7))
    assert delta.availability == "available"
    assert delta.confidence == "estimated"


def test_cross_clock_delta_is_unavailable_instead_of_subtracted() -> None:
    delta = comparable_delta(point(1_000, domain="a"), point(1, domain="b"))
    assert delta.availability == "unavailable"
    assert delta.nanoseconds is None
    assert delta.limitation == "cross_clock_domain"


def test_negative_same_clock_delta_is_inconsistent_not_clamped() -> None:
    delta = comparable_delta(point(10), point(9))
    assert delta.availability == "inconsistent"
    assert delta.nanoseconds is None


def test_turns_use_evidence_time_before_lexical_identifier(valid_bundle) -> None:
    template = valid_bundle.profile.operations[0]
    early = template.model_copy(
        update={
            "operation_id": "operation-turn-2",
            "turn_id": "turn-2",
            "span_id": "6" * 16,
            "started_at": point(1_000_000_000),
            "ended_at": point(1_100_000_000),
        }
    )
    later = template.model_copy(
        update={
            "operation_id": "operation-turn-10",
            "turn_id": "turn-10",
            "span_id": "7" * 16,
            "started_at": point(2_000_000_000),
            "ended_at": point(2_100_000_000),
        }
    )
    bundle = replace_profile(
        valid_bundle,
        operations=(later, early),
        events=(),
        quality_samples=(),
    )

    result = analyze(bundle)

    assert [item.turn_id for item in result.projections.turns] == ["turn-2", "turn-10"]


def test_incomparable_clock_groups_use_canonical_noncausal_order_in_analysis_and_explanation(
    valid_bundle,
) -> None:
    clocks = (
        ClockDomain(clock_domain_id="z-source-clock", kind="monotonic", observer="server"),
        ClockDomain(clock_domain_id="a-source-clock", kind="monotonic", observer="server"),
    )
    operations = (
        Operation(
            operation_id="op-z-late",
            session_id="session-1",
            operation_name="agent",
            status="ok",
            started_at=point(300, domain="z-source-clock"),
            ended_at=point(350, domain="z-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Operation(
            operation_id="op-a-late",
            session_id="session-1",
            operation_name="agent",
            status="ok",
            started_at=point(400, domain="a-source-clock"),
            ended_at=point(450, domain="a-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Operation(
            operation_id="op-observed-only",
            session_id="session-1",
            operation_name="agent",
            status="ok",
            started_at=TimePoint(observed_time_unix_nano="250"),
            turn_id="turn-mixed-clocks",
        ),
        Operation(
            operation_id="op-z-early",
            session_id="session-1",
            operation_name="agent",
            status="ok",
            started_at=point(100, domain="z-source-clock"),
            ended_at=point(150, domain="z-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Operation(
            operation_id="op-a-early",
            session_id="session-1",
            operation_name="agent",
            status="ok",
            started_at=point(200, domain="a-source-clock"),
            ended_at=point(250, domain="a-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
    )
    events = (
        Event(
            event_id="evt-z-late",
            session_id="session-1",
            event_name="earshot.test.marker",
            time=point(310, domain="z-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Event(
            event_id="evt-a-late",
            session_id="session-1",
            event_name="earshot.test.marker",
            time=point(410, domain="a-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Event(
            event_id="evt-observed-only",
            session_id="session-1",
            event_name="earshot.test.marker",
            time=TimePoint(observed_time_unix_nano="260"),
            turn_id="turn-mixed-clocks",
        ),
        Event(
            event_id="evt-z-early",
            session_id="session-1",
            event_name="earshot.test.marker",
            time=point(110, domain="z-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
        Event(
            event_id="evt-a-early",
            session_id="session-1",
            event_name="earshot.test.marker",
            time=point(210, domain="a-source-clock"),
            turn_id="turn-mixed-clocks",
        ),
    )
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, *clocks),
        operations=operations,
        events=events,
        quality_samples=(),
    )

    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    [projected_turn] = analysis.projections.turns
    assert projected_turn.operation_ids == (
        "op-a-early",
        "op-a-late",
        "op-z-early",
        "op-z-late",
        "op-observed-only",
    )
    assert projected_turn.event_ids == (
        "evt-a-early",
        "evt-a-late",
        "evt-z-early",
        "evt-z-late",
        "evt-observed-only",
    )

    explanation = explain_incident(bundle, analysis)
    [explained_turn] = explanation.turns
    assert tuple(item.operation_id for item in explained_turn.operations) == (
        "op-a-early",
        "op-a-late",
        "op-z-early",
        "op-z-late",
        "op-observed-only",
    )
    assert tuple(item.event_id for item in explained_turn.events) == (
        "evt-a-early",
        "evt-a-late",
        "evt-z-early",
        "evt-z-late",
        "evt-observed-only",
    )

    repeated_analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    assert repeated_analysis == analysis
    assert explain_incident(bundle, repeated_analysis) == explanation

    permuted = replace_profile(
        bundle,
        operations=tuple(reversed(bundle.profile.operations)),
        events=tuple(reversed(bundle.profile.events)),
    )
    permuted_analysis = analyze_incident(
        permuted,
        input_sha256=analysis.input_sha256,
        generated_at_unix_nano=analysis.generated_at_unix_nano,
    )
    assert permuted_analysis == analysis
    assert explain_incident(permuted, permuted_analysis) == explanation


def test_incomparable_turn_clock_groups_use_canonical_noncausal_order(valid_bundle) -> None:
    clocks = (
        ClockDomain(clock_domain_id="z-source-clock", kind="monotonic", observer="server"),
        ClockDomain(clock_domain_id="a-source-clock", kind="monotonic", observer="server"),
    )
    source_first = Operation(
        operation_id="op-z-late",
        session_id="session-1",
        operation_name="agent",
        status="ok",
        started_at=point(900, domain="z-source-clock"),
        ended_at=point(950, domain="z-source-clock"),
        turn_id="turn-z-late",
    )
    source_middle = Operation(
        operation_id="op-a-middle",
        session_id="session-1",
        operation_name="agent",
        status="ok",
        started_at=point(500, domain="a-source-clock"),
        ended_at=point(550, domain="a-source-clock"),
        turn_id="turn-a-middle",
    )
    source_last = Operation(
        operation_id="op-z-early",
        session_id="session-1",
        operation_name="agent",
        status="ok",
        started_at=point(100, domain="z-source-clock"),
        ended_at=point(150, domain="z-source-clock"),
        turn_id="turn-z-early",
    )
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, *clocks),
        operations=(source_first, source_middle, source_last),
        events=(),
        quality_samples=(),
    )

    result = analyze(bundle)

    assert [item.turn_id for item in result.projections.turns] == [
        "turn-a-middle",
        "turn-z-early",
        "turn-z-late",
    ]
    permuted = replace_profile(bundle, operations=tuple(reversed(bundle.profile.operations)))
    assert analyze(permuted) == result


def test_interruption_projection_uses_comparable_time_order(valid_bundle) -> None:
    template = valid_bundle.profile.events[0]
    detected = template.model_copy(
        update={
            "event_id": "evt-interruption-detected",
            "event_name": "earshot.interruption.detected",
            "time": point(1_100_000_000),
            "operation_id": None,
            "turn_id": "turn-1",
            "trace_id": None,
            "span_id": None,
        }
    )
    accepted = detected.model_copy(
        update={
            "event_id": "evt-interruption-accepted",
            "event_name": "earshot.interruption.accepted",
            "time": point(1_200_000_000),
        }
    )
    base_events = tuple(
        event
        for event in valid_bundle.profile.events
        if not event.event_name.startswith("earshot.interruption.")
    )
    bundle = replace_profile(valid_bundle, events=(*base_events, accepted, detected))

    result = analyze(bundle)
    projected = turn(result)["interruptions"]

    assert [item["event_name"] for item in projected] == [
        "earshot.interruption.detected",
        "earshot.interruption.accepted",
    ]
    permuted = replace_profile(bundle, events=tuple(reversed(bundle.profile.events)))
    assert analyze(permuted) == result


def test_high_fidelity_events_win_over_coarse_operation_starts(valid_bundle) -> None:
    result = analyze(valid_bundle)
    assert metric(result, "first_token_latency")["value"] == 150.0
    assert metric(result, "generated_response_latency")["value"] == 400.0
    assert metric(result, "response_latency")["value"] == 720.0
    assert metric(result, "response_latency")["basis"] == "render"
    assert metric(result, "response_latency")["evidence_ids"] == ["evt-turn", "evt-render"]


def test_response_falls_back_to_sent_only_with_explicit_estimate(bundle_factory) -> None:
    bundle = bundle_factory(include_render=False)
    result = analyze(bundle)
    response = metric(result, "response_latency")
    assert response["basis"] == "transport_estimate"
    assert response["value"] == 520.0
    assert response["evidence_ids"] == ["evt-turn", "evt-sent"]


def test_response_falls_back_to_tts_only_with_explicit_estimate(bundle_factory) -> None:
    bundle = bundle_factory(include_render=False)
    profile = bundle.profile
    events = tuple(item for item in profile.events if item.event_id != "evt-sent")
    operations = tuple(
        item for item in profile.operations if item.operation_name != "transport_send"
    )
    bundle = replace_profile(bundle, events=events, operations=operations)
    response = metric(analyze(bundle), "response_latency")
    assert response["basis"] == "tts_estimate"
    assert response["value"] == 400.0
    assert "heard" not in str(response).lower()


def test_missing_stage_is_not_observed_never_zero(bundle_factory) -> None:
    bundle = bundle_factory(include_render=False)
    bundle = replace_profile(
        bundle,
        operations=tuple(
            item
            for item in bundle.profile.operations
            if item.operation_name not in {"llm", "tts", "transport_send"}
        ),
        events=tuple(
            item
            for item in bundle.profile.events
            if item.event_name
            not in {
                "earshot.response.first_token",
                "earshot.response.first_audio_generated",
                "earshot.audio.first_byte_sent",
            }
        ),
    )
    metrics = turn(analyze(bundle))["metrics"]
    for name in ("first_token_latency", "generated_response_latency", "response_latency"):
        assert metrics[name]["availability"] == "not_observed"
        assert "value" not in metrics[name]


def test_provider_ttfb_and_turn_end_create_explicitly_estimated_projection(
    bundle_factory,
) -> None:
    bundle = bundle_factory(include_render=False)
    operations = []
    for operation in bundle.profile.operations:
        if operation.operation_name == "llm":
            operation = operation.model_copy(
                update={"attributes": {**operation.attributes, "metrics.ttfb": 0.1}}
            )
        elif operation.operation_name == "tts":
            operation = operation.model_copy(
                update={"attributes": {**operation.attributes, "metrics.ttfb": 0.05}}
            )
        operations.append(operation)
    bundle = replace_profile(bundle, operations=tuple(operations), events=())
    result = analyze(bundle)
    assert metric(result, "first_token_latency")["value"] == 150.0
    assert metric(result, "generated_response_latency")["value"] == 400.0
    assert metric(result, "response_latency")["value"] == 510.0
    assert metric(result, "response_latency")["basis"] == "transport_estimate"
    assert metric(result, "response_latency")["confidence"] == "estimated"


def test_failed_turn_detection_does_not_author_turn_anchor(valid_bundle) -> None:
    operations = tuple(
        operation.model_copy(update={"status": "failed"})
        if operation.operation_name == "turn_detection"
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(
        event
        for event in valid_bundle.profile.events
        if event.event_name not in {"earshot.turn.committed", "earshot.speech.ended"}
    )
    bundle = replace_profile(valid_bundle, operations=operations, events=events)
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, "first_token_latency")["availability"] == "not_observed"
    assert metric(result, "first_token_latency")["limitation"] == "turn_anchor_not_observed"


@pytest.mark.parametrize(
    ("operation_name", "event_name", "metric_name"),
    (
        ("transport_send", "earshot.audio.first_byte_sent", "sent_response_latency"),
        (
            "transport_receive",
            "earshot.audio.first_packet_received",
            "received_response_latency",
        ),
        ("render", "earshot.audio.render.started", "render_start_response_latency"),
    ),
)
def test_failed_operation_does_not_author_output_boundary(
    valid_bundle,
    operation_name: str,
    event_name: str,
    metric_name: str,
) -> None:
    operations = tuple(
        operation.model_copy(update={"status": "failed"})
        if operation.operation_name == operation_name
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(event for event in valid_bundle.profile.events if event.event_name != event_name)
    bundle = replace_profile(valid_bundle, operations=operations, events=events)
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, metric_name)["availability"] == "not_observed"


@pytest.mark.parametrize("status", ("canceled", "cancelled", "timed_out", "unknown"))
def test_non_success_status_spelling_fails_closed(valid_bundle, status: str) -> None:
    operations = tuple(
        operation.model_copy(update={"status": status})
        if operation.operation_name == "render"
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(
        event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.audio.render.started"
    )

    result = analyze(replace_profile(valid_bundle, operations=operations, events=events))

    assert metric(result, "render_start_response_latency")["availability"] == "not_observed"


def test_input_stream_transport_cannot_author_response_boundary(valid_bundle) -> None:
    operations = tuple(
        operation.model_copy(
            update={
                "participant_id": "participant-user",
                "stream_id": "stream-input",
                "started_at": point(1_100_000_000),
                "ended_at": point(1_200_000_000),
            }
        )
        if operation.operation_name == "transport_send"
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(
        event
        for event in valid_bundle.profile.events
        if event.event_name
        not in {
            "earshot.response.first_audio_generated",
            "earshot.audio.first_byte_sent",
            "earshot.audio.first_packet_received",
            "earshot.audio.render.started",
        }
    )
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, "sent_response_latency")["availability"] == "not_observed"
    assert metric(result, "response_latency")["basis"] != "transport_estimate"


def test_input_stream_output_event_cannot_author_response_boundary(valid_bundle) -> None:
    wrong_direction = next(
        event
        for event in valid_bundle.profile.events
        if event.event_name == "earshot.audio.first_byte_sent"
    ).model_copy(
        update={
            "operation_id": None,
            "participant_id": "participant-user",
            "stream_id": "stream-input",
        }
    )
    events = tuple(
        wrong_direction if event.event_id == wrong_direction.event_id else event
        for event in valid_bundle.profile.events
    )
    operations = tuple(
        operation.model_copy(update={"status": "failed"})
        if operation.operation_name == "transport_send"
        else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(valid_bundle, operations=operations, events=events)
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, "sent_response_latency")["availability"] == "not_observed"


def test_output_stream_speech_end_cannot_anchor_user_turn(valid_bundle) -> None:
    speech_end = next(
        event for event in valid_bundle.profile.events if event.event_name == "earshot.speech.ended"
    ).model_copy(
        update={
            "operation_id": None,
            "participant_id": "participant-agent",
            "stream_id": "stream-output",
        }
    )
    events = tuple(
        speech_end if event.event_id == speech_end.event_id else event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.turn.committed"
    )
    operations = tuple(
        operation.model_copy(update={"status": "failed"})
        if operation.operation_name == "turn_detection"
        else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(valid_bundle, operations=operations, events=events)
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, "first_token_latency")["availability"] == "not_observed"


def test_output_stream_turn_detection_cannot_anchor_user_turn(valid_bundle) -> None:
    operations = tuple(
        operation.model_copy(
            update={
                "participant_id": "participant-agent",
                "stream_id": "stream-output",
            }
        )
        if operation.operation_name == "turn_detection"
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(
        event
        for event in valid_bundle.profile.events
        if event.event_name not in {"earshot.turn.committed", "earshot.speech.ended"}
    )
    bundle = replace_profile(valid_bundle, operations=operations, events=events)
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, "first_token_latency")["availability"] == "not_observed"


@pytest.mark.parametrize(
    ("operation_name", "event_name", "metric_name"),
    (
        ("llm", "earshot.response.first_token", "first_token_latency"),
        (
            "tts",
            "earshot.response.first_audio_generated",
            "generated_response_latency",
        ),
    ),
)
def test_failed_provider_stage_does_not_author_latency_point(
    valid_bundle,
    operation_name: str,
    event_name: str,
    metric_name: str,
) -> None:
    operations = tuple(
        operation.model_copy(
            update={
                "status": "failed",
                "attributes": {**operation.attributes, "metrics.ttfb": 0.1},
            }
        )
        if operation.operation_name == operation_name
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(event for event in valid_bundle.profile.events if event.event_name != event_name)
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )
    assert validate_incident(bundle).ok

    result = analyze(bundle)

    assert metric(result, metric_name)["availability"] == "not_observed"


def test_provider_latency_point_cannot_exceed_operation_end(valid_bundle) -> None:
    operations = tuple(
        operation.model_copy(update={"attributes": {**operation.attributes, "metrics.ttfb": 1.0}})
        if operation.operation_name == "llm"
        else operation
        for operation in valid_bundle.profile.operations
    )
    events = tuple(
        event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.response.first_token"
    )
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )

    result = analyze(bundle)

    assert metric(result, "first_token_latency")["availability"] == "not_observed"


def test_latency_confidence_uses_weakest_boundary_evidence(valid_bundle) -> None:
    anchor = next(
        event
        for event in valid_bundle.profile.events
        if event.event_name == "earshot.turn.committed"
    )
    inferred_anchor = anchor.model_copy(
        update={
            "evidence": Evidence(
                source="test",
                observer="server",
                method="event_callback",
                confidence="inferred",
                availability="available",
            )
        }
    )
    inferred_bundle = replace_profile(
        valid_bundle,
        events=tuple(
            inferred_anchor if event.event_id == anchor.event_id else event
            for event in valid_bundle.profile.events
        ),
    )
    assert metric(analyze(inferred_bundle), "first_token_latency")["confidence"] == "inferred"

    target = next(
        event
        for event in valid_bundle.profile.events
        if event.event_name == "earshot.response.first_token"
    )
    unavailable_target = target.model_copy(
        update={
            "evidence": Evidence(
                source="test",
                observer="server",
                method="event_callback",
                confidence="unavailable",
                availability="available",
            )
        }
    )
    unavailable_bundle = replace_profile(
        valid_bundle,
        events=tuple(
            unavailable_target if event.event_id == target.event_id else event
            for event in valid_bundle.profile.events
        ),
    )
    assert metric(analyze(unavailable_bundle), "first_token_latency")["confidence"] == "unavailable"


@pytest.mark.parametrize("raw_ttfb", [1e308, 10**1000])
def test_untrusted_provider_duration_cannot_overflow_analysis(
    bundle_factory,
    raw_ttfb: float | int,
) -> None:
    bundle = bundle_factory(include_render=False)
    operations = tuple(
        operation.model_copy(
            update={"attributes": {**operation.attributes, "metrics.ttfb": raw_ttfb}}
        )
        if operation.operation_name == "llm"
        else operation
        for operation in bundle.profile.operations
    )
    bundle = replace_profile(bundle, operations=operations, events=())

    result = analyze(bundle)

    assert metric(result, "first_token_latency")["availability"] == "not_observed"
    assert "value" not in metric(result, "first_token_latency")


def test_analyzer_refuses_semantically_invalid_provider_measurement(valid_bundle) -> None:
    sample = QualitySample(
        sample_id="negative-response-latency",
        session_id="session-1",
        quality_kind="provider_latency",
        sample_window=TimeRange(start=point(1), end=point(1)),
        measurements=(
            QualityMeasurement(
                name="earshot.turn.response_latency",
                value=-250.0,
                unit="ms",
            ),
        ),
        evidence=Evidence(
            source="provider",
            observer="server",
            method="native_metric",
            confidence="measured",
            availability="available",
        ),
        attributes={"earshot.turn.id": "turn-1"},
    )
    result = analyze(replace_profile(valid_bundle, quality_samples=(sample,)))

    projected = metric(result, "provider_measurements")["earshot.turn.response_latency"]
    assert projected["availability"] == "unavailable"
    assert projected["limitation"] == "duration_or_latency_negative"
    assert "value" not in projected


def test_turn_id_propagates_through_complete_otel_parent_graph() -> None:
    from earshot.contract import IncidentProfile

    child = Operation(
        operation_id="child-llm",
        session_id="session-parent-turn",
        operation_name="llm",
        status="ok",
        started_at=point(1_100_000_000),
        ended_at=point(1_200_000_000),
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id="3" * 16,
        parent_scope="internal",
        attributes={"metrics.ttfb": 0.05},
    )
    parent = Operation(
        operation_id="parent-turn",
        session_id="session-parent-turn",
        operation_name="turn_detection",
        status="ok",
        started_at=point(900_000_000),
        ended_at=point(1_000_000_000),
        turn_id="turn-from-parent",
        trace_id="1" * 32,
        span_id="3" * 16,
        parent_scope="external",
    )
    from incident_factory import make_valid_bundle

    bundle = make_valid_bundle(session_id="session-parent-turn")
    profile = bundle.profile.model_copy(
        update={"operations": (child, parent), "events": (), "quality_samples": ()}
    )
    bundle = bundle.model_copy(update={"profile": IncidentProfile.model_validate(profile)})
    projection = analyze(bundle).projections["turns"][0]
    assert projection["turn_id"] == "turn-from-parent"
    assert projection["operation_ids"] == ["parent-turn", "child-llm"]


def test_combined_livekit_span_and_metric_uses_the_operation_with_provider_ttft(
    bundle_factory,
) -> None:
    bundle = bundle_factory(include_render=False)
    turn = next(
        operation
        for operation in bundle.profile.operations
        if operation.operation_name == "turn_detection"
    )
    llm = next(
        operation for operation in bundle.profile.operations if operation.operation_name == "llm"
    ).model_copy(update={"attributes": {"lk.response.ttft": 0.1}})
    agent_turn = llm.model_copy(
        update={
            "operation_id": "native-agent-turn",
            "operation_name": "agent",
            "span_id": "9" * 16,
            "attributes": {},
        }
    )
    bundle = replace_profile(bundle, operations=(turn, agent_turn, llm), events=())
    result = analyze(bundle)
    first_token = metric(result, "first_token_latency")
    assert first_token["value"] == 150.0
    assert first_token["evidence_ids"] == ["op-turn", "op-llm"]


def test_preemptive_livekit_first_token_uses_direct_stage_metric_when_delta_reversed(
    valid_bundle,
) -> None:
    events = tuple(
        event.model_copy(update={"time": point(1_200_000_000)})
        if event.event_name == "earshot.turn.committed"
        else event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.response.first_token"
    )
    operations = tuple(
        operation.model_copy(
            update={"attributes": {**operation.attributes, "lk.response.ttft": 0.1}}
        )
        if operation.operation_name == "llm"
        else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    first_token = metric(result, "first_token_latency")

    assert first_token == {
        "availability": "available",
        "basis": "provider_stage_direct",
        "confidence": "measured",
        "value": 100.0,
        "unit": "ms",
        "limitation": "stage_local_excludes_turn_scheduling",
        "evidence_ids": ["op-llm"],
    }
    assert validate_derived_analysis(bundle, result).ok


def test_provider_stage_fallback_uses_the_bounded_attribute_that_authored_target(
    valid_bundle,
) -> None:
    events = tuple(
        event.model_copy(update={"time": point(1_200_000_000)})
        if event.event_name == "earshot.turn.committed"
        else event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.response.first_token"
    )
    operations = tuple(
        operation.model_copy(
            update={
                "attributes": {
                    **operation.attributes,
                    "lk.response.ttft": 1.0,
                    "metrics.ttfb": 0.1,
                }
            }
        )
        if operation.operation_name == "llm"
        else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )
    assert validate_incident(bundle).ok

    first_token = metric(analyze(bundle), "first_token_latency")

    assert first_token["basis"] == "provider_stage_direct"
    assert first_token["value"] == 100.0
    assert first_token["evidence_ids"] == ["op-llm"]


def test_provider_stage_fallback_downgrades_unavailable_source_evidence(valid_bundle) -> None:
    events = tuple(
        event.model_copy(update={"time": point(1_200_000_000)})
        if event.event_name == "earshot.turn.committed"
        else event
        for event in valid_bundle.profile.events
        if event.event_name != "earshot.response.first_token"
    )
    operations = tuple(
        operation.model_copy(
            update={
                "attributes": {**operation.attributes, "metrics.ttfb": 0.1},
                "evidence": operation.evidence.model_copy(
                    update={"availability": "unavailable", "confidence": "measured"}
                )
                if operation.evidence is not None
                else None,
            }
        )
        if operation.operation_name == "llm"
        else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(
        valid_bundle,
        operations=operations,
        events=events,
        quality_samples=(),
    )
    assert validate_incident(bundle).ok

    first_token = metric(analyze(bundle), "first_token_latency")

    assert first_token["availability"] == "available"
    assert first_token["confidence"] == "unavailable"


@pytest.mark.parametrize(
    ("llm_name", "tts_name"),
    [
        ("livekit.llm_node_ttft", "livekit.tts_node_ttfb"),
        ("pipecat.llm.ttfb", "pipecat.tts.ttfb"),
    ],
)
def test_provider_stage_latency_aliases_share_projections_without_turn_anchor(
    valid_bundle,
    llm_name: str,
    tts_name: str,
) -> None:
    samples = tuple(
        QualitySample(
            sample_id=f"provider-stage-{stage}",
            session_id="session-1",
            quality_kind="pipeline.latency",
            sample_window=TimeRange(start=point(index), end=point(index)),
            measurements=(QualityMeasurement(name=name, value=value, unit="s"),),
            evidence=Evidence(
                source="provider",
                observer="server",
                method="native_metric",
                confidence="measured",
                availability="available",
            ),
            attributes={"earshot.turn.id": "turn-1"},
        )
        for index, (stage, name, value) in enumerate(
            (("llm", llm_name, 0.12), ("tts", tts_name, 0.23)),
            start=1,
        )
    )
    bundle = replace_profile(valid_bundle, events=(), quality_samples=samples)

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    metrics = turn(result)["metrics"]

    assert metrics["first_token_latency"]["value"] == 120.0
    assert metrics["generated_response_latency"]["value"] == 230.0
    for name in ("first_token_latency", "generated_response_latency"):
        assert metrics[name]["basis"] == "provider_stage_direct"
        assert metrics[name]["limitation"] == "stage_local_excludes_turn_scheduling"
    assert validate_derived_analysis(bundle, result).ok


def test_cross_domain_render_does_not_create_false_exact_response_latency(valid_bundle) -> None:
    browser_clock = ClockDomain(clock_domain_id="browser-clock", kind="browser", observer="client")
    events = list(valid_bundle.profile.events)
    index = next(i for i, item in enumerate(events) if item.event_id == "evt-render")
    events[index] = events[index].model_copy(update={"time": point(1, domain="browser-clock")})
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, browser_clock),
        events=tuple(events),
    )
    response = metric(analyze(bundle), "response_latency")
    assert response["availability"] == "unavailable"
    assert "value" not in response


def test_parallel_tool_work_and_elapsed_wall_time_are_separate(valid_bundle) -> None:
    tools = (
        Operation(
            operation_id="tool-a",
            session_id="session-1",
            operation_name="tool",
            status="ok",
            started_at=point(1_100_000_000),
            ended_at=point(1_300_000_000),
            turn_id="turn-1",
        ),
        Operation(
            operation_id="tool-b",
            session_id="session-1",
            operation_name="tool",
            status="ok",
            started_at=point(1_200_000_000),
            ended_at=point(1_400_000_000),
            turn_id="turn-1",
        ),
    )
    bundle = replace_profile(valid_bundle, operations=(*valid_bundle.profile.operations, *tools))
    tool_metrics = metric(analyze(bundle), "tools")
    assert tool_metrics["operation_count"] == 2
    assert tool_metrics["timed_operation_count"] == 2
    assert tool_metrics["untimed_operation_count"] == 0
    assert tool_metrics["total_work_completeness"] == "complete"
    assert "limitation" not in tool_metrics
    assert tool_metrics["total_work_ms"] == 400.0
    assert tool_metrics["elapsed_ms_by_clock_domain"] == {"server-clock": {"monotonic": 300.0}}

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    [projected_turn] = result.projections.turns
    for forged_elapsed in ({"server-clock": {"monotonic": 1.0}}, {}):
        forged_tools = projected_turn.metrics.tools.model_copy(
            update={"elapsed_ms_by_clock_domain": forged_elapsed}
        )
        forged_metrics = projected_turn.metrics.model_copy(update={"tools": forged_tools})
        forged_turn = projected_turn.model_copy(update={"metrics": forged_metrics})
        forged_projections = result.projections.model_copy(update={"turns": (forged_turn,)})
        report = validate_derived_analysis(
            bundle,
            result.model_copy(update={"projections": forged_projections}),
        )
        assert "EARSHOT_ANALYSIS_TOOL_MISMATCH" in {issue.code for issue in report.errors}


@pytest.mark.parametrize("elapsed_ms", [-1.0, float("inf"), float("nan")])
def test_tool_elapsed_values_must_be_finite_and_nonnegative(elapsed_ms: float) -> None:
    with pytest.raises(ValueError):
        ToolAnalysis.model_validate(
            {
                "operation_count": 1,
                "timed_operation_count": 1,
                "untimed_operation_count": 0,
                "total_work_ms": 1.0,
                "elapsed_ms_by_clock_domain": {
                    "clock": {"monotonic": elapsed_ms},
                },
                "evidence_ids": ["tool"],
            }
        )


def test_tool_elapsed_basis_map_cannot_be_empty() -> None:
    with pytest.raises(ValueError):
        ToolAnalysis.model_validate(
            {
                "operation_count": 1,
                "timed_operation_count": 1,
                "untimed_operation_count": 0,
                "total_work_ms": 1.0,
                "elapsed_ms_by_clock_domain": {"clock": {}},
                "evidence_ids": ["tool"],
            }
        )


def test_open_tool_duration_is_unavailable_not_observed_zero(valid_bundle) -> None:
    open_tool = Operation(
        operation_id="tool-open",
        session_id="session-1",
        operation_name="tool",
        status="unset",
        started_at=point(1_100_000_000),
        turn_id="turn-1",
    )
    bundle = replace_profile(
        valid_bundle,
        operations=(*valid_bundle.profile.operations, open_tool),
    )

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    tools = metric(result, "tools")

    assert tools == {
        "operation_count": 1,
        "timed_operation_count": 0,
        "untimed_operation_count": 1,
        "total_work_ms": 0.0,
        "total_work_completeness": "unavailable",
        "limitation": "incomplete_tool_intervals",
        "elapsed_ms_by_clock_domain": {},
        "evidence_ids": ["tool-open"],
    }
    assert validate_derived_analysis(bundle, result).ok

    [projected_turn] = result.projections.turns
    forged_tools = projected_turn.metrics.tools.model_copy(
        update={
            "timed_operation_count": 1,
            "untimed_operation_count": 0,
            "total_work_completeness": "complete",
            "limitation": None,
        }
    )
    forged_metrics = projected_turn.metrics.model_copy(update={"tools": forged_tools})
    forged_turn = projected_turn.model_copy(update={"metrics": forged_metrics})
    forged_projections = result.projections.model_copy(update={"turns": (forged_turn,)})
    report = validate_derived_analysis(
        bundle,
        result.model_copy(update={"projections": forged_projections}),
    )
    assert "EARSHOT_ANALYSIS_TOOL_MISMATCH" in {issue.code for issue in report.errors}


def test_mixed_tool_durations_report_only_known_work_as_partial(valid_bundle) -> None:
    known = Operation(
        operation_id="tool-known",
        session_id="session-1",
        operation_name="tool",
        status="ok",
        started_at=point(1_100_000_000),
        ended_at=point(1_200_000_000),
        turn_id="turn-1",
    )
    unknown = known.model_copy(
        update={
            "operation_id": "tool-unknown",
            "status": "unset",
            "started_at": point(1_300_000_000),
            "ended_at": None,
        }
    )
    result = analyze(
        replace_profile(
            valid_bundle,
            operations=(*valid_bundle.profile.operations, known, unknown),
        )
    )
    tools = metric(result, "tools")

    assert tools["timed_operation_count"] == 1
    assert tools["untimed_operation_count"] == 1
    assert tools["total_work_ms"] == 100.0
    assert tools["total_work_completeness"] == "partial"
    assert tools["limitation"] == "incomplete_tool_intervals"


def test_failed_retry_attempt_remains_evidence_linked(valid_bundle) -> None:
    failed = Operation(
        operation_id="tool-failed-attempt",
        session_id="session-1",
        operation_name="tool",
        status="timeout",
        started_at=point(1_100_000_000),
        ended_at=point(1_200_000_000),
        turn_id="turn-1",
    )
    bundle = replace_profile(valid_bundle, operations=(*valid_bundle.profile.operations, failed))
    result = analyze(bundle)
    diagnosis = next(item for item in result.diagnoses if item.code == "operation.failed")
    assert diagnosis.evidence_refs == ("tool-failed-attempt",)
    assert diagnosis.confidence == "measured"


def test_max_length_failed_operation_id_produces_a_bounded_diagnosis_id(
    valid_bundle,
) -> None:
    operation_id = "x" * 256
    failed = Operation(
        operation_id=operation_id,
        session_id="session-1",
        operation_name="tool",
        status="failed",
        started_at=point(1_100_000_000),
        ended_at=point(1_200_000_000),
        turn_id="turn-1",
    )
    result = analyze(
        replace_profile(valid_bundle, operations=(*valid_bundle.profile.operations, failed))
    )
    diagnosis = next(item for item in result.diagnoses if item.evidence_refs == (operation_id,))
    assert len(diagnosis.diagnosis_id) <= 256
    assert diagnosis.diagnosis_id.startswith("operation_failed.")


def test_tool_elapsed_clock_key_preserves_a_clock_domain_containing_colons(
    valid_bundle,
) -> None:
    clock_domain_id = "runtime:worker:clock"
    clock = ClockDomain(
        clock_domain_id=clock_domain_id,
        kind="monotonic",
        observer="server",
    )
    tool = Operation(
        operation_id="tool-colon-clock",
        session_id="session-1",
        operation_name="tool",
        status="ok",
        started_at=point(1_100_000_000, domain=clock_domain_id),
        ended_at=point(1_200_000_000, domain=clock_domain_id),
        turn_id="turn-1",
    )
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, clock),
        operations=(*valid_bundle.profile.operations, tool),
    )
    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    assert metric(result, "tools")["elapsed_ms_by_clock_domain"] == {
        clock_domain_id: {"monotonic": 100.0}
    }
    assert validate_derived_analysis(bundle, result).ok


def test_tool_elapsed_groups_keep_domain_and_basis_structural(valid_bundle) -> None:
    wall_clock = ClockDomain(
        clock_domain_id="runtime:worker:clock",
        kind="wall",
        observer="server",
    )
    colliding_clock = ClockDomain(
        clock_domain_id="runtime:worker:clock:monotonic",
        kind="monotonic",
        observer="server",
    )
    wall_start = TimePoint(
        source_time_unix_nano="1800000000100000000",
        clock_domain_id=wall_clock.clock_domain_id,
    )
    wall_end = TimePoint(
        source_time_unix_nano="1800000000200000000",
        clock_domain_id=wall_clock.clock_domain_id,
    )
    tools = (
        Operation(
            operation_id="tool-monotonic",
            session_id="session-1",
            operation_name="tool",
            status="ok",
            started_at=point(400_000_000, domain=wall_clock.clock_domain_id),
            ended_at=point(425_000_000, domain=wall_clock.clock_domain_id),
            turn_id="turn-1",
        ),
        Operation(
            operation_id="tool-wall",
            session_id="session-1",
            operation_name="tool",
            status="ok",
            started_at=wall_start,
            ended_at=wall_end,
            turn_id="turn-1",
        ),
        Operation(
            operation_id="tool-colon-domain",
            session_id="session-1",
            operation_name="tool",
            status="ok",
            started_at=point(300_000_000, domain=colliding_clock.clock_domain_id),
            ended_at=point(350_000_000, domain=colliding_clock.clock_domain_id),
            turn_id="turn-1",
        ),
    )
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, wall_clock, colliding_clock),
        operations=(*valid_bundle.profile.operations, *tools),
    )

    result = analyze(bundle)

    assert metric(result, "tools")["elapsed_ms_by_clock_domain"] == {
        "runtime:worker:clock": {"monotonic": 25.0, "source_wall": 100.0},
        "runtime:worker:clock:monotonic": {"monotonic": 50.0},
    }


def test_cross_clock_tool_interval_does_not_author_elapsed_time(valid_bundle) -> None:
    other_clock = ClockDomain(
        clock_domain_id="other-clock",
        kind="monotonic",
        observer="server",
    )
    tool = Operation(
        operation_id="tool-cross-clock",
        session_id="session-1",
        operation_name="tool",
        status="ok",
        started_at=point(100_000_000),
        ended_at=point(200_000_000, domain=other_clock.clock_domain_id),
        turn_id="turn-1",
    )
    cross_basis_tool = Operation(
        operation_id="tool-cross-basis",
        session_id="session-1",
        operation_name="tool",
        status="ok",
        started_at=TimePoint(monotonic_time_nano="300000000", clock_domain_id="server-clock"),
        ended_at=TimePoint(
            source_time_unix_nano="1800000000400000000",
            clock_domain_id="server-clock",
        ),
        turn_id="turn-1",
    )
    bundle = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, other_clock),
        operations=(*valid_bundle.profile.operations, tool, cross_basis_tool),
    )

    assert validate_incident(bundle).ok
    tools = metric(analyze(bundle), "tools")
    assert tools["timed_operation_count"] == 0
    assert tools["elapsed_ms_by_clock_domain"] == {}


def test_reversed_tool_interval_does_not_author_elapsed_time(valid_bundle) -> None:
    tool = Operation(
        operation_id="tool-reversed",
        session_id="session-1",
        operation_name="tool",
        status="ok",
        started_at=point(200_000_000),
        ended_at=point(100_000_000),
        turn_id="turn-1",
    )
    bundle = replace_profile(
        valid_bundle,
        operations=(*valid_bundle.profile.operations, tool),
    )

    assert "EARSHOT_TIME_RANGE_REVERSED" in {
        issue.code for issue in validate_incident(bundle).errors
    }
    tools = metric(analyze(bundle), "tools")
    assert tools["timed_operation_count"] == 0
    assert tools["elapsed_ms_by_clock_domain"] == {}


def test_native_speech_to_speech_without_serial_stages_still_projects(bundle_factory) -> None:
    bundle = bundle_factory(include_render=True)
    bundle = replace_profile(
        bundle,
        operations=tuple(
            item
            for item in bundle.profile.operations
            if item.operation_name in {"turn_detection", "render"}
        ),
        events=tuple(
            item
            for item in bundle.profile.events
            if item.event_name
            in {
                "earshot.turn.committed",
                "earshot.response.first_audio_generated",
                "earshot.audio.render.started",
            }
        ),
    )
    # Remove references to omitted serial-stage operations while retaining native events.
    events = tuple(item.model_copy(update={"operation_id": None}) for item in bundle.profile.events)
    result = analyze(replace_profile(bundle, events=events))
    assert result.projections["summary"]["turn_count"] == 1
    assert metric(result, "first_token_latency")["availability"] == "not_observed"
    assert metric(result, "response_latency")["basis"] == "render"


def test_analysis_is_invariant_to_operation_and_event_arrival_order(valid_bundle) -> None:
    expected = analyze(valid_bundle).model_dump(mode="python")
    variants = (
        replace_profile(
            valid_bundle,
            operations=tuple(reversed(valid_bundle.profile.operations)),
            events=tuple(reversed(valid_bundle.profile.events)),
        ),
        replace_profile(
            valid_bundle,
            operations=valid_bundle.profile.operations[2:] + valid_bundle.profile.operations[:2],
            events=valid_bundle.profile.events[3:] + valid_bundle.profile.events[:3],
        ),
    )
    for variant in variants:
        assert analyze(variant).model_dump(mode="python") == expected


def test_conflicting_render_coverage_is_order_invariant(valid_bundle) -> None:
    coverage = (
        Coverage(
            signal="render",
            availability="not_observed",
            reason="generic render signal was not captured",
        ),
        Coverage(
            signal="client.render",
            availability="unavailable",
            reason="client render instrumentation was unavailable",
        ),
    )
    bundle = replace_profile(
        valid_bundle,
        operations=tuple(
            operation
            for operation in valid_bundle.profile.operations
            if operation.operation_name != "render"
        ),
        events=tuple(
            event
            for event in valid_bundle.profile.events
            if event.event_name != "earshot.audio.render.started"
        ),
        coverage=coverage,
    )

    result = analyze(bundle)
    permuted = replace_profile(bundle, coverage=tuple(reversed(coverage)))

    assert analyze(permuted) == result
    assert result.projections.limitations == ("render_evidence_conflicting_coverage",)


def test_conflicting_snapshots_in_one_sample_are_order_invariant(valid_bundle) -> None:
    sample = QualitySample(
        sample_id="provider-snapshot",
        session_id="session-1",
        quality_kind="provider.metric",
        sample_window=TimeRange(start=point(10), end=point(10)),
        measurements=(
            QualityMeasurement(
                name="provider.queue_depth",
                value=10,
                unit="{item}",
                aggregation="instant",
            ),
            QualityMeasurement(
                name="provider.queue_depth",
                value=20,
                unit="{item}",
                aggregation="instant",
            ),
        ),
        evidence=Evidence(
            source="provider",
            observer="server",
            method="native_metric",
            confidence="measured",
            availability="available",
        ),
        attributes={"earshot.turn.id": "turn-1"},
    )
    bundle = replace_profile(valid_bundle, quality_samples=(sample,))
    permuted = replace_profile(
        bundle,
        quality_samples=(
            sample.model_copy(update={"measurements": tuple(reversed(sample.measurements))}),
        ),
    )

    result = analyze(bundle)
    permuted_result = analyze(permuted)

    assert permuted_result == result
    assert metric(result, "provider_measurements")["provider.queue_depth"] == {
        "availability": "unavailable",
        "basis": "provider_measurement",
        "confidence": "unavailable",
        "limitation": "ambiguous_measurements_in_sample",
        "evidence_ids": ["provider-snapshot"],
    }
    assert explain_incident(permuted, permuted_result) == explain_incident(bundle, result)


def test_provider_measurement_confidence_requires_available_evidence(valid_bundle) -> None:
    sample = QualitySample(
        sample_id="provider-unavailable-evidence",
        session_id="session-1",
        quality_kind="provider.metric",
        sample_window=TimeRange(start=point(10), end=point(10)),
        measurements=(
            QualityMeasurement(
                name="provider.queue_depth",
                value=10,
                unit="{item}",
                aggregation="instant",
            ),
        ),
        evidence=Evidence(
            source="provider",
            observer="server",
            method="native_metric",
            confidence="measured",
            availability="unavailable",
        ),
        attributes={"earshot.turn.id": "turn-1"},
    )

    projected = metric(
        analyze(replace_profile(valid_bundle, quality_samples=(sample,))),
        "provider_measurements",
    )["provider.queue_depth"]

    assert projected["availability"] == "available"
    assert projected["confidence"] == "unavailable"


def test_turn_delta_quality_windows_are_summed_with_all_evidence(valid_bundle) -> None:
    samples = (
        QualitySample(
            sample_id="vad-window-b",
            session_id="session-1",
            quality_kind="pipeline.metric",
            sample_window=TimeRange(start=point(20), end=point(20)),
            measurements=(
                QualityMeasurement(
                    name="earshot.duration.inference_seconds",
                    value=0.02,
                    unit="s",
                    aggregation="delta",
                ),
            ),
            evidence=Evidence(
                source="livekit",
                observer="server",
                method="metrics_listener",
                confidence="measured",
                availability="available",
            ),
            attributes={"earshot.turn.id": "turn-1"},
        ),
        QualitySample(
            sample_id="vad-window-a",
            session_id="session-1",
            quality_kind="pipeline.metric",
            sample_window=TimeRange(start=point(10), end=point(10)),
            measurements=(
                QualityMeasurement(
                    name="earshot.duration.inference_seconds",
                    value=0.01,
                    unit="s",
                    aggregation="delta",
                ),
            ),
            evidence=Evidence(
                source="livekit",
                observer="server",
                method="metrics_listener",
                confidence="measured",
                availability="available",
            ),
            attributes={"earshot.turn.id": "turn-1"},
        ),
    )
    bundle = replace_profile(valid_bundle, quality_samples=samples)
    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    provider = metric(result, "provider_measurements")
    inference = provider["earshot.duration.inference_seconds"]
    assert inference == {
        "availability": "available",
        "basis": "provider_delta_sum",
        "confidence": "measured",
        "value": 30.0,
        "unit": "ms",
        "evidence_ids": ["vad-window-a", "vad-window-b"],
    }
    assert validate_derived_analysis(bundle, result).ok

    reversed_bundle = replace_profile(valid_bundle, quality_samples=tuple(reversed(samples)))
    reversed_result = analyze_incident(
        reversed_bundle,
        input_sha256=analysis_input_sha256(reversed_bundle),
        generated_at_unix_nano="1800000005000000000",
    )
    assert metric(reversed_result, "provider_measurements") == provider


def test_turn_delta_quality_overflow_is_unavailable_not_analyzer_failure(valid_bundle) -> None:
    samples = tuple(
        QualitySample(
            sample_id=f"huge-delta-{suffix}",
            session_id="session-1",
            quality_kind="pipeline.metric",
            sample_window=TimeRange(start=point(index), end=point(index)),
            measurements=(
                QualityMeasurement(
                    name="earshot.metric.future_total",
                    value=1e308,
                    unit="1",
                    aggregation="delta",
                ),
            ),
            evidence=Evidence(
                source="provider",
                observer="server",
                method="metrics_listener",
                confidence="measured",
                availability="available",
            ),
            attributes={"earshot.turn.id": "turn-1"},
        )
        for index, suffix in enumerate(("a", "b"), start=1)
    )
    bundle = replace_profile(valid_bundle, quality_samples=samples)

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )

    assert metric(result, "provider_measurements")["earshot.metric.future_total"] == {
        "availability": "unavailable",
        "basis": "provider_delta_sum",
        "confidence": "unavailable",
        "limitation": "delta_sum_not_finite",
        "evidence_ids": ["huge-delta-a", "huge-delta-b"],
    }
    assert validate_derived_analysis(bundle, result).ok


def test_integer_delta_quality_is_exact_and_rejects_out_of_i_json_domain(valid_bundle) -> None:
    maximum = 9_007_199_254_740_991
    samples = tuple(
        QualitySample(
            sample_id=f"{name}-{index}",
            session_id="session-1",
            quality_kind="pipeline.metric",
            sample_window=TimeRange(start=point(index), end=point(index)),
            measurements=(
                QualityMeasurement(
                    name=name,
                    value=value,
                    unit="count",
                    aggregation="delta",
                ),
            ),
            evidence=Evidence(
                source="provider",
                observer="server",
                method="metrics_listener",
                confidence="measured",
                availability="available",
            ),
            attributes={"earshot.turn.id": "turn-1"},
        )
        for name, values in (
            ("earshot.metric.exact_counter", (maximum - 1, 1)),
            ("earshot.metric.overflow_counter", (maximum, 2)),
        )
        for index, value in enumerate(values, start=1)
    )
    bundle = replace_profile(valid_bundle, quality_samples=samples)

    result = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )

    provider = metric(result, "provider_measurements")
    exact = provider["earshot.metric.exact_counter"]
    assert exact["value"] == maximum
    assert isinstance(exact["value"], int)
    assert provider["earshot.metric.overflow_counter"] == {
        "availability": "unavailable",
        "basis": "provider_delta_sum",
        "confidence": "unavailable",
        "limitation": "delta_sum_outside_i_json_domain",
        "evidence_ids": [
            "earshot.metric.overflow_counter-1",
            "earshot.metric.overflow_counter-2",
        ],
    }
    assert validate_derived_analysis(bundle, result).ok


def test_direct_analysis_ignores_an_oversized_quality_integer(valid_bundle) -> None:
    sample = QualitySample(
        sample_id="oversized-quality-integer",
        session_id="session-1",
        quality_kind="pipeline.metric",
        sample_window=TimeRange(start=point(1), end=point(1)),
        measurements=(
            QualityMeasurement(
                name="earshot.metric.oversized_counter",
                value=10**1_000,
                unit="count",
                aggregation="delta",
            ),
        ),
        attributes={"earshot.turn.id": "turn-1"},
    )
    bundle = replace_profile(valid_bundle, quality_samples=(sample,))

    result = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000005000000000",
    )

    assert "earshot.metric.oversized_counter" not in metric(
        result,
        "provider_measurements",
    )


def test_equal_timestamps_use_stable_event_id_tiebreaker(valid_bundle) -> None:
    first = Event(
        event_id="evt-token-a",
        session_id="session-1",
        event_name="earshot.response.first_token",
        time=point(1_100_000_000),
        turn_id="turn-1",
    )
    second = first.model_copy(update={"event_id": "evt-token-b"})
    events = (
        *(
            item
            for item in valid_bundle.profile.events
            if item.event_name != "earshot.response.first_token"
        ),
        second,
        first,
    )
    result = analyze(replace_profile(valid_bundle, events=events))
    assert metric(result, "first_token_latency")["evidence_ids"][-1] == "evt-token-a"


def test_analysis_does_not_mutate_source_bundle(valid_bundle) -> None:
    before = deepcopy(valid_bundle.model_dump(mode="python"))
    analyze(valid_bundle)
    assert valid_bundle.model_dump(mode="python") == before


def test_every_diagnosis_is_backed_by_an_existing_evidence_record(bundle_factory) -> None:
    bundle = bundle_factory(include_render=False)
    result = analyze(bundle)
    evidence_ids = {
        *(item.operation_id for item in bundle.profile.operations),
        *(item.event_id for item in bundle.profile.events),
        *(item.sample_id for item in bundle.profile.quality_samples),
        *(item.media_id for item in bundle.profile.media_refs),
    }
    for diagnosis in result.diagnoses:
        assert diagnosis.evidence_refs, diagnosis
        assert set(diagnosis.evidence_refs) <= evidence_ids
