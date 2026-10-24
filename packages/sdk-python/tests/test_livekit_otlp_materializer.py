"""Pure half: a LiveKit call's stored spans as a revision and a transcript.

Every test drives the real LiveKit Agents 1.8.8 capture (``fixtures/captured/livekit-otlp-1.8``)
decoded as the receiver decodes it and stored as the receiver stores it. The server path
(``materialize``) dispatches the ``livekit-agents`` scope to the same ``LiveKitAdapter`` the
in-process span processor feeds, through the one stored-span conversion
(``adapter_span``); the two must derive the same turn facts. Transcript lines follow the
rule in ``call_materializer``: LiveKit user lines are ``user_turn``'s transcript, agent
lines ``agent_turn``'s played text, never an ``llm_request`` output.
"""

from __future__ import annotations

import hashlib

import pytest

from adapter_conformance import assert_capture_conforms, assert_same_turn_facts, turn_facts
from captured_livekit_otlp import livekit_requests, livekit_spans, readable_span
from earshot.adapters.livekit import LiveKitAdapter
from earshot.call_capture import (
    apply_capture_preset,
    resource_payload,
    span_payload,
    spans_from_request,
    stored_span,
)
from earshot.call_materializer import (
    CallSnapshot,
    call_clock_domain,
    capture_policy,
    materialize,
    transcript_lines,
    transcript_text_json,
)
from earshot.clock import ManualClock
from earshot.recorder import IncidentRecorder, RecorderConfig
from otlp_support import string_attribute

pytestmark = pytest.mark.unit

CALL_ID = "call_fedcba9876543210fedcba9876543210"
USER_LINES = [
    "Hi. I would like to book a table for two people tomorrow evening.",
    "Seven thirty works for us. Could we sit somewhere quiet near the window?",
    "Perfect. That is everything I needed. Thank you very much. Goodbye.",
]
# What the agent played (``agent_turn``), not the generations it discarded.
AGENT_LINES = [
    "Sure, I'd be happy to arrange that. What time would you prefer for tomorrow evening?",
    "Got it—table for two at 7:30 tomorrow, near the window. We'll reserve a quiet spot for you.",
    "You're welcome! See you tomorrow at 7:30. Have a great day!",
]
# ``gen_ai.output.messages`` of the preemptive generations LiveKit discarded: billed,
# never spoken.
DISCARDED = [
    "Got it—table for two at 7:30 PM tomorrow. Looking forward to seeing you!",
    "All set. See you tomorrow at 7:30. Enjoy your evening!",
]


def _stored(spans, preset: str = "full"):
    """The spans as the receiver stores them under ``preset`` and the closer reads them."""

    kept = [apply_capture_preset(preset, span) for span in spans]
    return [stored_span(span_payload(span.span), resource_payload(span)) for span in kept]


def _snapshot(spans=None, *, preset: str = "full", final: bool = True) -> CallSnapshot:
    return CallSnapshot(
        call_id=CALL_ID,
        revision=1,
        final=final,
        capture_preset=preset,
        spans=tuple(_stored(livekit_spans() if spans is None else spans, preset)),
        recordings=(),
    )


def _in_process_bundle(preset: str = "full"):
    """The capture through the in-process adapter, on a recorder with the revision's
    capture policy and clock domain (provider-metric sample IDs name the domain)."""

    spans = livekit_spans()
    recorder = IncidentRecorder(
        session_id="in-process",
        bundle_id="in-process-call",
        clock=ManualClock(wall=min(span.span.start_time_unix_nano for span in spans)),
        config=RecorderConfig(
            clock_domain_id=call_clock_domain(CALL_ID), capture_policy=capture_policy(preset)
        ),
    )
    adapter = LiveKitAdapter(recorder)
    for span in spans:  # in export order, as a span processor sees them end
        adapter.consume_span(readable_span(span))
    return recorder.close()


# --- dispatch ------------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_a_livekit_revision_is_built_by_the_livekit_adapter(preset) -> None:
    spans = livekit_spans()
    bundle = assert_capture_conforms(
        lambda: materialize(_snapshot(preset=preset)), require_omission=False
    )
    profile = bundle.profile

    assert [adapter.name for adapter in profile.manifest.adapters] == ["earshot.livekit"]
    assert len(profile.operations) == 58
    assert {(op.trace_id, op.span_id) for op in profile.operations} == {
        (span.span.trace_id.hex(), span.span.span_id.hex()) for span in spans
    }
    coverage = {item.signal: item for item in profile.coverage}
    assert "call.spans" not in coverage  # no scope went unrecognized
    assert coverage["call.transcript"].availability == (
        "available" if preset == "full" else "unavailable"
    )


def test_the_server_revision_derives_the_turn_facts_the_in_process_adapter_derives() -> None:
    served = materialize(_snapshot())
    in_process = _in_process_bundle()

    assert_same_turn_facts(served, in_process)
    assert [fact[0] for fact in turn_facts(served)] == [
        "speech_a0d5ed4ef063",
        "speech_8812b055897c",
        "speech_5241735f09f9",
    ]


def test_the_revision_omits_llm_input_history_and_declares_it() -> None:
    bundle = materialize(_snapshot())
    llm = [op for op in bundle.profile.operations if op.operation_name == "llm"]
    omitted = {
        omission.attributes["field_key_sha256"]: omission.capture_class
        for omission in bundle.profile.privacy.omissions
        if "field_key_sha256" in omission.attributes
    }

    assert len(llm) == 15  # five each of llm_node, llm_request and llm_request_run
    assert not any(
        key in {"gen_ai.input", "gen_ai.input.messages"} for op in llm for key in op.attributes
    )
    for history in ("gen_ai.input.messages", "lk.pii.chat_ctx"):
        assert omitted[hashlib.sha256(history.encode()).hexdigest()] == "model_payload"
    # The played reply is kept, under its semantic key.
    agent_turns = [
        op.attributes.get("gen_ai.output.messages")
        for op in bundle.profile.operations
        if op.attributes.get("earshot.framework.operation.name") == "agent_turn"
    ]
    assert agent_turns == AGENT_LINES


def test_under_metadata_only_the_revision_holds_no_livekit_content() -> None:
    encoded = materialize(_snapshot(preset="metadata_only")).model_dump_json()

    room_names = ('"earshot-livekit-otlp"', '"console"')  # as JSON string values
    for text in (*USER_LINES, *AGENT_LINES, *DISCARDED, *room_names):
        assert text not in encoded


# --- transcript lines ---------------------------------------------------------------------


def _lines(spans):
    return [(line.speaker, line.text) for line in transcript_lines(spans)]


def test_livekit_lines_are_the_user_transcripts_and_the_played_agent_text() -> None:
    assert _lines(_stored(livekit_spans())) == [
        line
        for pair in zip(USER_LINES, AGENT_LINES, strict=True)
        for line in (("user", pair[0]), ("agent", pair[1]))
    ]


def test_no_line_comes_from_llm_output_so_discarded_generations_are_never_lines() -> None:
    spans = _stored(livekit_spans())
    texts = [text for _, text in _lines(spans)]
    llm_outputs = [span for span in spans if span.span.name == "llm_request"]

    assert len(llm_outputs) == 5  # every generation, the two discarded ones included
    assert not set(DISCARDED) & set(texts)
    assert all(transcript_text_json(span) is None for span in llm_outputs)


def test_each_line_carries_its_span_and_its_span_times() -> None:
    spans = {span.span.span_id.hex(): span for span in _stored(livekit_spans())}

    for line in transcript_lines(spans.values()):
        span = spans[line.span_id]
        assert span.span.name == ("user_turn" if line.speaker == "user" else "agent_turn")
        assert (line.start_unix_nano, line.end_unix_nano) == (
            span.span.start_time_unix_nano,
            span.span.end_time_unix_nano,
        )


def _renamed(old: str, new: str):
    """The capture with every ``old`` attribute renamed ``new`` (a 1.6 spelling)."""

    requests = livekit_requests()
    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    for item in span.attributes:
                        if item.key == old:
                            item.key = new
    return [span for request in requests for span in spans_from_request(request)]


def test_the_1_6_spellings_give_the_same_lines_parsed_by_attribute_not_version() -> None:
    spans = _renamed("lk.pii.user_transcript", "lk.user_transcript")
    requests_spans = _renamed("lk.pii.response.text", "lk.response.text")
    by_id = {span.span.span_id: span for span in requests_spans}
    mixed = [by_id[span.span.span_id] if span.span.name == "agent_turn" else span for span in spans]

    assert _lines(_stored(mixed)) == _lines(_stored(livekit_spans()))


def test_under_metadata_only_livekit_spans_carry_no_line() -> None:
    assert transcript_lines(_stored(livekit_spans(), "metadata_only")) == []


def test_a_blank_or_absent_livekit_text_gives_no_line() -> None:
    [user_turn] = [span for span in livekit_spans() if span.span.name == "user_turn"][:1]
    attributes = user_turn.span.attributes
    for index in reversed(range(len(attributes))):
        if attributes[index].key == "lk.pii.user_transcript":
            del attributes[index]
    attributes.append(string_attribute("lk.pii.user_transcript", "   "))

    assert transcript_text_json(user_turn) is None
