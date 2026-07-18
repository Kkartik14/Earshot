"""The provider-neutral pipeline facade must produce analyzable, honest incidents."""

from __future__ import annotations

import pytest

import earshot
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.storage import IncidentStore
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

START = 1_752_800_000_000_000_000


def _two_turn_session() -> earshot.PipelineSession:
    sess = earshot.pipeline(session_id="custom-call", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.vad(speech_end_ms=0)
        turn.stt("deepgram", model="nova-3", ttfb_ms=180, final_ms=420)
        turn.llm("openai", model="gpt-4o", ttft_ms=350, completion_ms=600)
        turn.tts("cartesia", model="sonic-3", ttfb_ms=90, first_audio_ms=140)
    with sess.turn() as turn:
        turn.stt("deepgram", model="nova-3", ttfb_ms=200, final_ms=450)
        turn.llm("openai", model="gpt-4o", ttft_ms=410)
        turn.tts("cartesia", model="sonic-3", ttfb_ms=105)
        turn.barge_in(at_ms=120, accepted=True)
    return sess


def test_custom_pipeline_incident_is_contract_valid() -> None:
    bundle = _two_turn_session().close()

    report = validate_incident(bundle)
    assert report.ok, [issue.code for issue in report.errors]
    assert bundle.profile.manifest.adapters[0].framework == "custom_pipeline"
    operation_names = {operation.operation_name for operation in bundle.profile.operations}
    assert {"stt", "llm", "tts"} <= operation_names


def test_provider_reported_latencies_populate_turn_facts(tmp_path) -> None:
    bundle = _two_turn_session().close()

    # Derived first-token/generated latency come straight from the facade's
    # earshot.llm.ttft / earshot.tts.ttfb measurements.
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    first_turn = analysis.projections.turns[0].metrics
    assert first_turn.first_token_latency.value == pytest.approx(350)
    assert first_turn.first_token_latency.confidence == "measured"
    assert first_turn.generated_response_latency.value == pytest.approx(90)

    store = IncidentStore(tmp_path)
    store.create_project("pipe", display_name="Pipe")
    store.ingest(bundle, project_id="pipe")
    facts = store.list_turn_facts(project_id="pipe")

    assert len(facts) == 2
    assert facts[0].framework == "custom_pipeline"
    assert facts[0].provider == "openai"
    assert facts[0].model == "gpt-4o"
    assert facts[0].first_token_ms == pytest.approx(350)
    assert facts[0].first_token_confidence == "measured"
    assert facts[0].generated_response_ms == pytest.approx(90)


def test_barge_in_authors_an_accepted_interruption(tmp_path) -> None:
    bundle = _two_turn_session().close()

    interruptions = [
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.interruption.accepted"
    ]
    assert len(interruptions) == 1
    assert interruptions[0].turn_id == "turn-1"

    store = IncidentStore(tmp_path)
    store.create_project("pipe", display_name="Pipe")
    store.ingest(bundle, project_id="pipe")
    facts = store.list_turn_facts(project_id="pipe")
    assert facts[1].interruption_count == 1


def test_estimated_latency_is_not_labelled_measured() -> None:
    sess = earshot.pipeline(session_id="wallclock-call", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.llm("groq", model="llama-3.1-8b", ttft_ms=300, confidence="estimated")
    bundle = sess.close()

    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    metric = analysis.projections.turns[0].metrics.first_token_latency
    assert metric.value == pytest.approx(300)
    assert metric.confidence == "estimated"
    llm_operation = next(
        operation for operation in bundle.profile.operations if operation.operation_name == "llm"
    )
    assert llm_operation.evidence.source == "app"


def test_provider_scalar_does_not_fabricate_a_measured_stage_interval() -> None:
    sess = earshot.pipeline(session_id="honest-stage", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.llm(
            "openai",
            model="gpt-4o",
            ttft_ms=350,
            completion_ms=600,
            confidence="measured",
        )
    bundle = sess.close()

    [operation] = bundle.profile.operations
    [sample] = bundle.profile.quality_samples
    assert operation.ended_at is None
    assert operation.evidence is not None
    assert operation.evidence.source == "app"
    assert operation.evidence.confidence == "inferred"
    assert sample.evidence.source == "provider"
    assert sample.evidence.confidence == "measured"


def test_barge_in_offset_is_relative_to_the_turn() -> None:
    sess = earshot.pipeline(session_id="barge-clock", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.llm("openai", ttft_ms=350, completion_ms=600)
        turn.tts("cartesia", ttfb_ms=90, first_audio_ms=140)
        turn.barge_in(at_ms=1600)
    bundle = sess.close()

    event = next(
        item
        for item in bundle.profile.events
        if item.event_name == "earshot.interruption.accepted"
    )
    assert int(event.time.source_time_unix_nano) == START + 1_600_000_000


def test_stage_arguments_are_validated_before_recording() -> None:
    sess = earshot.pipeline(session_id="atomic-stage", started_at_unix_nano=START)
    with sess.turn() as turn, pytest.raises(ValueError, match="non-negative"):
        turn.llm("openai", ttft_ms=-1, completion_ms=600)
    bundle = sess.close()

    assert bundle.profile.operations == ()
    assert bundle.profile.quality_samples == ()


def test_first_audio_boundary_is_preserved_without_provider_ttfb() -> None:
    sess = earshot.pipeline(session_id="first-audio", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.vad(speech_end_ms=0)
        turn.tts("cartesia", first_audio_ms=140, confidence="estimated")
    bundle = sess.close()

    first_audio = next(
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.response.first_audio_generated"
    )
    assert first_audio.evidence.confidence == "estimated"
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    assert analysis.projections.turns[0].metrics.generated_response_latency.value == 140


def test_final_transcript_is_attributed_to_the_user() -> None:
    sess = earshot.pipeline(session_id="speaker", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.stt("deepgram", final_ms=420)
    bundle = sess.close()

    [event] = bundle.profile.events
    assert event.event_name == "earshot.transcript.final"
    assert event.participant_id == "participant-user"


def test_failed_turn_does_not_reuse_its_clock_origin() -> None:
    sess = earshot.pipeline(session_id="turn-failure", started_at_unix_nano=START)
    with pytest.raises(RuntimeError, match="application failure"), sess.turn(
        "failed"
    ) as turn:
        turn.llm("openai", ttft_ms=100)
        raise RuntimeError("application failure")
    with sess.turn("next") as turn:
        turn.llm("openai", ttft_ms=100)
    bundle = sess.close()

    failed, following = bundle.profile.operations
    assert int(following.started_at.monotonic_time_nano) > int(
        failed.started_at.monotonic_time_nano
    )


def test_custom_recorder_config_keeps_pipeline_adapter_identity() -> None:
    config = earshot.RecorderConfig(producer_name="customer-app", producer_version="2.0")
    sess = earshot.pipeline(
        session_id="custom-config",
        started_at_unix_nano=START,
        framework="raw_websocket",
        config=config,
    )
    bundle = sess.close()

    assert bundle.profile.manifest.producer.name == "customer-app"
    assert any(
        adapter.name == "earshot.pipeline" and adapter.framework == "raw_websocket"
        for adapter in bundle.profile.manifest.adapters
    )


def test_tts_voice_is_retained_as_governed_metadata() -> None:
    sess = earshot.pipeline(session_id="voice", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.tts("cartesia", model="sonic-3", voice="helpful-assistant")
    bundle = sess.close()

    [operation] = bundle.profile.operations
    assert operation.attributes["earshot.tts.voice"] == "helpful-assistant"


def test_vad_offsets_are_relative_to_the_turn() -> None:
    sess = earshot.pipeline(session_id="vad-clock", started_at_unix_nano=START)
    with sess.turn() as turn:
        turn.llm("openai", completion_ms=600)
        turn.vad(speech_start_ms=100, speech_end_ms=1700)
    bundle = sess.close()

    started, ended = bundle.profile.events
    assert int(started.time.source_time_unix_nano) == START + 100_000_000
    assert int(ended.time.source_time_unix_nano) == START + 1_700_000_000


def test_duplicate_explicit_turn_ids_are_rejected() -> None:
    sess = earshot.pipeline(session_id="turn-ids", started_at_unix_nano=START)
    with sess.turn("same"):
        pass
    with pytest.raises(ValueError, match="unique"), sess.turn("same"):
        pass


def test_advanced_authoring_keeps_each_fact_evidence_independent() -> None:
    sess = earshot.pipeline(session_id="native-facts", started_at_unix_nano=START)
    with sess.turn() as turn:
        operation_id = turn.record_stage(
            "agent", "openai", model="gpt-realtime", at_ms=100
        )
        turn.record_measurement(
            "earshot.turn.response_latency",
            410,
            unit="ms",
            operation_id=operation_id,
            source="app",
            confidence="estimated",
            source_field="response.output_audio.delta",
            basis="server_vad_stop_receipt_to_first_audio_receipt",
            at_ms=510,
        )
        turn.record_event(
            "earshot.audio.first_packet_received",
            at_ms=510,
            participant="agent",
            source="app",
            confidence="estimated",
            source_field="response.output_audio.delta",
        )
    bundle = sess.close()

    [sample] = bundle.profile.quality_samples
    assert sample.evidence.source == "app"
    assert sample.evidence.source_field == "response.output_audio.delta"
    assert sample.attributes["earshot.metric.basis"] == (
        "server_vad_stop_receipt_to_first_audio_receipt"
    )
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    assert analysis.projections.turns[0].metrics.response_latency.value == 410


def test_closed_session_rejects_new_turns() -> None:
    sess = earshot.pipeline(session_id="closed-call", started_at_unix_nano=START)
    sess.close()
    with pytest.raises(RuntimeError), sess.turn():
        pass
