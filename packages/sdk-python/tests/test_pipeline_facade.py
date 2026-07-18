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


def test_closed_session_rejects_new_turns() -> None:
    sess = earshot.pipeline(session_id="closed-call", started_at_unix_nano=START)
    sess.close()
    with pytest.raises(RuntimeError), sess.turn():
        pass
