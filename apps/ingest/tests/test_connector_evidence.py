"""A delivery's evidence as spans: identity, the digest, and what a preset keeps.

Pure: no store. Each rule below is what lets two deliveries of one call share
spans without a conflict (a shared span is byte-identical whichever event carried
it), what makes a retry recognisable after an upgrade or with re-signed URLs (the
digest covers the provider's facts and the recording locators, nothing else), and
what keeps a URL or a transcript out of a ``metadata_only`` catalog.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from earshot.call_capture import (
    CAPTURE_PRESET_METADATA_ONLY,
    apply_capture_preset,
    attribute_values,
    is_end_marker,
    resolve_session_key,
    resource_payload,
    span_payload,
)
from earshot.connectors.evidence import (
    CALL,
    TRANSCRIPT_LINE,
    TRANSLATOR,
    Evidence,
    EvidenceSpan,
    RecordingSource,
    otlp_spans,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
URL = "https://storage.example/rec.wav?X-Amz-Signature=CANARY0123456789"


def _evidence(*, url: str = URL, extra: tuple[EvidenceSpan, ...] = ()) -> Evidence:
    return Evidence(
        provider="retell",
        provider_call_id="call_abc123",
        spans=(
            EvidenceSpan(CALL, 0, START, START + 30 * SECOND, {"gen_ai.agent.id": "agent_1"}),
            EvidenceSpan(
                TRANSCRIPT_LINE,
                0,
                START + SECOND,
                START + 2 * SECOND,
                {
                    "earshot.connector.transcript.speaker": "agent",
                    "earshot.connector.transcript.text": "Hello, this is the clinic.",
                    "earshot.connector.transcript.timing": "measured",
                    "earshot.connector.transcript.index": 0,
                },
            ),
            *extra,
        ),
        recording_sources=(
            RecordingSource(
                track="mixed",
                speaker="mixed",
                content_type="audio/wav",
                started_at_unix_nano=START,
                url=url,
            ),
        ),
    )


def test_spans_are_keyed_by_kind_and_ordinal_on_one_trace_per_session_key() -> None:
    first = otlp_spans(_evidence(), session_key="retell:call_abc123", translator_version="1.0")
    again = otlp_spans(_evidence(), session_key="retell:call_abc123", translator_version="1.0")
    merged = otlp_spans(_evidence(), session_key="call_abc123", translator_version="1.0")

    assert [span_payload(span.span) for span in first] == [
        span_payload(span.span) for span in again
    ]
    assert len({bytes(span.span.trace_id) for span in first}) == 1
    assert len({bytes(span.span.span_id) for span in first}) == len(first)
    # Another key is another call's trace, so a key change never meets a bound trace.
    assert first[0].span.trace_id != merged[0].span.trace_id
    call = next(span for span in first if span.span.name == CALL)
    assert all(span.span.parent_span_id == call.span.span_id for span in first if span is not call)
    assert {resolve_session_key(span).value for span in first} == {"retell:call_abc123"}
    assert [span.scope.name for span in first] == ["earshot.connector.retell"] * len(first)


def test_a_span_shared_by_two_events_is_byte_identical_and_the_version_has_its_own_span() -> None:
    older = otlp_spans(_evidence(), session_key="retell:call_abc123", translator_version="1.0")
    newer = otlp_spans(_evidence(), session_key="retell:call_abc123", translator_version="2.0")

    def by_name(spans):
        return {
            (span.span.name, bytes(span.span.span_id)): (
                span_payload(span.span),
                resource_payload(span),
            )
            for span in spans
        }

    shared = by_name(older).keys() & by_name(newer).keys()
    assert {name for name, _ in shared} == {CALL, TRANSCRIPT_LINE}
    assert all(by_name(older)[key] == by_name(newer)[key] for key in shared)
    versions = {
        attribute_values(span.span.attributes)["earshot.connector.translator.version"]
        for span in (*older, *newer)
        if span.span.name == TRANSLATOR
    }
    assert versions == {"1.0", "2.0"}


def test_the_call_span_ends_the_call_only_for_a_terminal_delivery() -> None:
    ended = otlp_spans(_evidence(), session_key="k", translator_version="1.0")
    progress = otlp_spans(
        replace(_evidence(), terminal=False), session_key="k", translator_version="1.0"
    )

    assert [span.span.name for span in ended if is_end_marker(span)] == [CALL]
    assert [span.span.name for span in progress if is_end_marker(span)] == []


def test_the_digest_covers_facts_and_locators_but_not_urls_versions_or_keys() -> None:
    base = _evidence()
    re_signed = _evidence(url="https://storage.example/rec.wav?X-Amz-Signature=OTHER")
    more = _evidence(extra=(EvidenceSpan("earshot.connector.analysis", 0, START, START, {"x": 1}),))

    assert base.digest() == re_signed.digest()
    assert base.digest() != more.digest()
    assert len(base.digest()) == 64
    # A different track is a different recording, so a different delivery.
    other_track = replace(
        base,
        recording_sources=(RecordingSource("customer", "user", "audio/wav", START, url=URL),),
    )
    assert other_track.digest() != base.digest()


def test_no_url_reaches_a_span_and_metadata_only_keeps_no_text() -> None:
    spans = otlp_spans(_evidence(), session_key="retell:call_abc123", translator_version="1.0")
    stored = b"".join(span_payload(span.span) + resource_payload(span) for span in spans)
    assert b"CANARY" not in stored and b"storage.example" not in stored

    kept = [apply_capture_preset(CAPTURE_PRESET_METADATA_ONLY, span) for span in spans]
    kept_bytes = b"".join(span_payload(span.span) + resource_payload(span) for span in kept)
    assert b"clinic" not in kept_bytes
    assert b"call_abc123" not in kept_bytes  # the session key is never kept
    assert b"agent_1" in kept_bytes  # an agent ID is metadata


def test_every_connector_span_kind_and_attribute_is_registered_in_semconv() -> None:
    from earshot.connectors import evidence

    registry = yaml.safe_load((ROOT / "semconv" / "earshot.yaml").read_text())
    groups = {group["id"]: group for group in registry["groups"]}
    registered = {attribute["id"] for attribute in groups["registry.earshot"]["attributes"]}
    attributes = {
        value
        for name, value in vars(evidence).items()
        if name.isupper()
        and isinstance(value, str)
        and value.startswith("earshot.connector.")
        and value not in evidence.SPAN_KINDS
    }

    assert len(attributes) > 20, "the vocabulary constants are missing"
    assert attributes - registered == set()
    assert {f"span.{kind}" for kind in evidence.SPAN_KINDS} <= groups.keys()
    for kind in evidence.SPAN_KINDS:
        assert groups[f"span.{kind}"]["type"] == "span"
