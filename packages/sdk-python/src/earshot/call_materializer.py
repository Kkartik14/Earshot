"""A call's stored evidence as a revision bundle (FR-7), and as transcript lines (AC7.3).

Pure: it reads nothing and writes nothing. The closer (``storage_closer``) takes a
snapshot of a call's spans and stored recordings and hands it here; the receiver
asks here for each span's transcript line as it stores the span.

**Transcript lines** come from one rule, ``transcript_text``. A user line is
every Pipecat ``stt`` span with a non-blank ``transcript`` (``is_final`` is not a
filter: it was false on every ``stt`` span of the real 1.5 capture). An agent
line is a Pipecat ``llm`` span's ``output``, which is complete on every Pipecat
version and keeps partial output on interruption; ``tts.text`` holds only the
last sentence on 1.5 and is never used. A cancelled ``llm`` span has no output
and gives no line. ``"NOT_GIVEN"`` is an absent value. The receiver stores each
span's line when it stores the span (``spans.text_json``), so the read API reads
a call's transcript without its spans' payloads; a revision reads it from the
spans it is built from (``transcript_lines``).

**A revision** (``materialize``) is built the way the hosted connectors build a
bundle on the server: an ``IncidentRecorder`` on a ``ManualClock`` set from the
evidence (first start to last end; never the server clock), bundle ID
``<call_id>.r<n>``, session ID ``call_id``, clock domain ``otlp-<call_id>``, and
a manual trace ID derived from the bundle ID, so one snapshot always gives the
same bytes. Spans go in ``(start, trace_id, span_id)`` order to the adapter their
instrumentation scope names (``_ADAPTERS_BY_SCOPE``: Pipecat in Month 1); spans
of other scopes stay in the catalog and are declared, not bundled.

What a span carries reaches the bundle only under the capture class the receiver
gave it (``call_capture.attribute_capture_class``): an attribute the recorder
would file under another class, and LLM input history (the whole conversation,
again, on every ``llm`` span), are left out with a declared omission, so the
output and transcript fit the recorder's value budget and the bundle grows
linearly with the call. Full fidelity stays in ``spans``.

Each stored recording becomes a ``MediaRef`` held by ``earshot.blob_store``
(its sha256 and size, its speaker's stream), on its own media clock domain with a
``ClockRelation`` from ``started_at_unix_nano`` to the call's clock. An
end-signal close is ``final``; any other close is a partial observation
(``close_partial``, ``otlp_quiet_period``/``no_end_signal``, ``interrupted``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .adapters.pipecat import PipecatAdapter, native_attribute_name
from .call_capture import (
    CAPTURE_PRESET_FULL,
    CAPTURE_PRESET_METADATA_ONLY,
    NOT_GIVEN,
    OtlpSpan,
    any_value,
    attribute_capture_class,
    attribute_values,
    canonical_attribute_key,
)
from .clock import ManualClock
from .contract import (
    ClockDomain,
    ClockRelation,
    IncidentBundle,
    MediaRef,
    Producer,
    RecoveryRecord,
    TimePoint,
    TimeRange,
)
from .privacy import CaptureClass, CapturePolicy, classify_attribute
from .recorder import IncidentRecorder, RecorderConfig
from .versions import PACKAGE_VERSION

if TYPE_CHECKING:
    from .storage_recordings import Recording

PRODUCER_NAME = "earshot.closer"
MEDIA_CUSTODIAN = "earshot.blob_store"
# The most recordings a revision holds: each takes one of the recorder's 16 extra
# clock domains (its media timeline). The API refuses a call's 17th recording
# (``EARSHOT_RECORDING_LIMIT``); a call that has more anyway keeps its first ones by
# creation and declares the others omitted.
MAX_REVISION_RECORDINGS = 16
FULL_CAPTURE_POLICY_ID = "earshot.preset.full"
_NANOS_PER_MILLISECOND = 1_000_000
# The canonical name of LLM input history (Pipecat's ``input``, OpenInference's
# ``input.value``, OTel's ``gen_ai.input.messages``), and the in-process Pipecat
# adapter's own name for it.
_INPUT_HISTORY_KEYS = frozenset({"gen_ai.input.messages", "gen_ai.input"})
# A recording's speaker: its participant, the participant's role, its stream, and
# the stream's direction as seen from the agent.
_SPEAKERS = {
    "user": ("participant-user", "user", "stream-user", "inbound"),
    "agent": ("participant-agent", "agent", "stream-agent", "outbound"),
    "mixed": ("participant-call", "call", "stream-mixed", "mixed"),
}
# OTLP ``Status.code`` values, by the names the adapters read.
_STATUS_NAMES = {0: "unset", 1: "ok", 2: "error"}
# The instrumentation scopes Pipecat's tracers emit (``trace.get_tracer("pipecat")``
# and ``("pipecat.turn")``, verified on the real 1.5 capture).
PIPECAT_SCOPES = frozenset({"pipecat", "pipecat.turn"})


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One line of a call's transcript, and the span it came from."""

    speaker: str  # "user" or "agent"
    text: str
    start_unix_nano: int
    end_unix_nano: int
    trace_id: str
    span_id: str


def _span_order(span: OtlpSpan) -> tuple[int, bytes, bytes]:
    return (span.span.start_time_unix_nano, span.span.trace_id, span.span.span_id)


# The span kinds that carry a transcript line, by span name (in ``PIPECAT_SCOPES``):
# ``(speaker, attribute)``. Readers may select only these spans before asking
# ``transcript_lines`` for the lines.
TRANSCRIPT_SPAN_SOURCES = {"stt": ("user", "transcript"), "llm": ("agent", "output")}
# ``transcript_coverage`` values: lines found; the preset stores none; none observed.
TRANSCRIPT_AVAILABLE = "available"
TRANSCRIPT_NOT_CAPTURED = "not_captured_by_preset"
TRANSCRIPT_NOT_OBSERVED = "not_observed"


def _line_source(span: OtlpSpan) -> tuple[str, str] | None:
    """``(speaker, attribute)`` of the span kinds that carry a transcript line."""

    if span.scope.name not in PIPECAT_SCOPES:
        return None
    return TRANSCRIPT_SPAN_SOURCES.get(span.span.name)


def _attribute_value(attributes: Iterable[Any], key: str) -> Any:
    """``attribute_values(attributes).get(key)``, converting only ``key``'s value: an
    ``llm`` span's other attributes include the whole conversation (``input``)."""

    for item in attributes:
        if item.key != key:
            continue
        value = any_value(item.value)
        if value is not None and value != NOT_GIVEN:
            return value
    return None


def transcript_text(span: OtlpSpan) -> tuple[str, str] | None:
    """``(speaker, text)`` of the transcript line a span carries, or ``None``.

    The one rule for what a line is: the transcript keeps these lines (FR-10), and
    so does a revision's transcript coverage. The receiver extracts it when it
    stores a span (``transcript_text_json``), so reading a transcript never
    parses a stored span.
    """

    source = _line_source(span)
    if source is None:
        return None
    speaker, attribute = source
    text = _attribute_value(span.span.attributes, attribute)
    if not isinstance(text, str) or not text.strip():
        return None
    return speaker, text


def transcript_text_json(span: OtlpSpan) -> str | None:
    """The span's line as ``spans.text_json`` stores it (``None``: no line)."""

    line = transcript_text(span)
    if line is None:
        return None
    speaker, text = line
    return json.dumps({"speaker": speaker, "text": text}, separators=(",", ":"))


def stored_transcript_line(
    text_json: str, *, start_unix_nano: int, end_unix_nano: int, trace_id: bytes, span_id: bytes
) -> TranscriptLine:
    """A ``spans.text_json`` value and its span's columns as a transcript line."""

    line = json.loads(text_json)
    return TranscriptLine(
        speaker=line["speaker"],
        text=line["text"],
        start_unix_nano=start_unix_nano,
        end_unix_nano=end_unix_nano,
        trace_id=trace_id.hex(),
        span_id=span_id.hex(),
    )


def transcript_order(line: TranscriptLine) -> tuple[int, str, str]:
    """Span start order, then trace and span ID (hex sorts as the bytes do)."""

    return (line.start_unix_nano, line.trace_id, line.span_id)


def transcript_lines(spans: Iterable[OtlpSpan]) -> list[TranscriptLine]:
    """The call's transcript, in span start order (then trace and span ID)."""

    lines: list[TranscriptLine] = []
    for span in spans:
        line = transcript_text(span)
        if line is None:
            continue
        speaker, text = line
        lines.append(
            TranscriptLine(
                speaker=speaker,
                text=text,
                start_unix_nano=span.span.start_time_unix_nano,
                end_unix_nano=span.span.end_time_unix_nano,
                trace_id=span.span.trace_id.hex(),
                span_id=span.span.span_id.hex(),
            )
        )
    return sorted(lines, key=transcript_order)


def adapter_span(span: OtlpSpan) -> dict[str, Any]:
    """A stored span as the dict a framework adapter's ``consume_span`` reads.

    The one conversion from the receiver's protobuf form to the adapters' input:
    hex IDs, integer nanosecond times, events with ``timestamp``, the status by
    name, attributes as a mapping (``"NOT_GIVEN"`` absent), and the resource and
    instrumentation scope the span was exported under.
    """

    raw = span.span
    return {
        "name": raw.name,
        "trace_id": raw.trace_id.hex(),
        "span_id": raw.span_id.hex(),
        "parent_span_id": raw.parent_span_id.hex() or None,
        "start_time": raw.start_time_unix_nano,
        "end_time": raw.end_time_unix_nano,
        "status": _STATUS_NAMES.get(raw.status.code, "unset"),
        "attributes": attribute_values(raw.attributes),
        "events": [
            {
                "name": event.name,
                "timestamp": event.time_unix_nano,
                "attributes": attribute_values(event.attributes),
            }
            for event in raw.events
        ],
        "links": [
            {
                "context": {"trace_id": link.trace_id.hex(), "span_id": link.span_id.hex()},
                "attributes": attribute_values(link.attributes),
            }
            for link in raw.links
        ],
        "resource": {
            "attributes": attribute_values(span.resource.attributes),
            "schema_url": span.resource_schema_url or None,
        },
        "instrumentation_scope": {
            "name": span.scope.name,
            "version": span.scope.version or None,
            "attributes": attribute_values(span.scope.attributes),
            "schema_url": span.scope_schema_url or None,
        },
    }


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    """What one revision is built from: the call's spans and stored recordings as
    the closer's claim saw them, and how the call closed."""

    call_id: str
    revision: int
    # Whether an end signal was observed (the revision is final).
    final: bool
    capture_preset: str
    spans: tuple[OtlpSpan, ...]
    recordings: tuple[Recording, ...]


def revision_bundle_id(call_id: str, revision: int) -> str:
    return f"{call_id}.r{revision}"


def call_clock_domain(call_id: str) -> str:
    return f"otlp-{call_id}"


def capture_policy(preset: str) -> CapturePolicy:
    """The recorder policy a project's capture preset maps to."""

    if preset == CAPTURE_PRESET_FULL:
        return CapturePolicy(
            enabled=frozenset(item for item in CaptureClass if item is not CaptureClass.RAW_OTLP),
            policy_id=FULL_CAPTURE_POLICY_ID,
        )
    if preset == CAPTURE_PRESET_METADATA_ONLY:
        return CapturePolicy.metadata_only()
    raise ValueError(f"unknown capture preset {preset!r}")


def materialize(snapshot: CallSnapshot) -> IncidentBundle:
    """Build revision ``snapshot.revision`` of the call (deterministic, no I/O)."""

    spans = sorted(snapshot.spans, key=_span_order)
    by_creation = sorted(
        snapshot.recordings, key=lambda item: (item.created_unix_nano, item.recording_id)
    )
    omitted_recordings = by_creation[MAX_REVISION_RECORDINGS:]
    recordings = sorted(
        by_creation[:MAX_REVISION_RECORDINGS],
        key=lambda item: (int(item.started_at_unix_nano), item.recording_id),
    )
    if not spans and not recordings:
        raise ValueError("a revision needs a span or a stored recording")
    start, end = _evidence_window(spans, recordings)
    clock = ManualClock(wall=start, monotonic=0)
    bundle_id = revision_bundle_id(snapshot.call_id, snapshot.revision)
    domain = call_clock_domain(snapshot.call_id)
    policy = capture_policy(snapshot.capture_preset)
    recorder = IncidentRecorder(
        session_id=snapshot.call_id,
        bundle_id=bundle_id,
        clock=clock,
        config=RecorderConfig(
            producer_name=PRODUCER_NAME,
            clock_domain_id=domain,
            capture_policy=policy,
        ),
        manual_trace_id=hashlib.sha256(
            f"earshot.call-revision\x00{bundle_id}".encode()
        ).hexdigest()[:32],
    )
    for recording in recordings:
        _add_recording(recorder, recording, domain, policy)
    if omitted_recordings:
        recorder.record_coverage(
            "call.recordings",
            "partial",
            "recording_limit",
            dropped_count=len(omitted_recordings),
        )
        for recording in omitted_recordings:
            recorder.record_omission(
                f"recording.{recording.recording_id}", capture_class=CaptureClass.AUDIO
            )
    adapters: dict[type, Any] = {}
    unrecognized: dict[str, int] = {}
    for span in spans:
        dispatch = _ADAPTERS_BY_SCOPE.get(span.scope.name)
        if dispatch is None:
            unrecognized[span.scope.name] = unrecognized.get(span.scope.name, 0) + 1
            continue
        adapter_type, adapter_input = dispatch
        if adapter_type not in adapters:
            adapters[adapter_type] = adapter_type(recorder)
        adapters[adapter_type].consume_span(adapter_input(recorder, span))
    if unrecognized:
        recorder.record_coverage(
            "call.spans",
            "partial",
            "unrecognized_instrumentation_scope",
            dropped_count=sum(unrecognized.values()),
        )
        for scope in sorted(unrecognized):
            recorder.record_omission(f"otlp.scope.{scope}", capture_class=CaptureClass.METADATA)
    _record_transcript_coverage(recorder, spans, snapshot.capture_preset)
    clock.wall, clock.monotonic = end, end - start
    if snapshot.final:
        return recorder.close("completed")
    return recorder.close_partial(
        RecoveryRecord(
            method="otlp_quiet_period",
            reason="no_end_signal",
            close_observed=False,
            recoverer=Producer(name=PRODUCER_NAME, version=PACKAGE_VERSION),
        ),
        status="interrupted",
    )


def _recording_end(recording: Recording) -> int:
    started = int(recording.started_at_unix_nano)
    if recording.duration_ms is None:
        return started
    return started + recording.duration_ms * _NANOS_PER_MILLISECOND


def _evidence_window(spans: Sequence[OtlpSpan], recordings: Sequence[Recording]) -> tuple[int, int]:
    """The first start and the last end of the call's evidence."""

    starts = [span.span.start_time_unix_nano for span in spans]
    starts += [int(recording.started_at_unix_nano) for recording in recordings]
    ends = [span.span.end_time_unix_nano for span in spans]
    ends += [_recording_end(recording) for recording in recordings]
    return min(starts), max(ends)


def _pipecat_input(recorder: IncidentRecorder, span: OtlpSpan) -> dict[str, Any]:
    """The span as the Pipecat adapter consumes it, keeping only attributes the
    bundle records under the class the receiver gives them (the rest declared)."""

    converted = adapter_span(span)
    kept: dict[str, Any] = {}
    for key, value in converted["attributes"].items():
        expected = attribute_capture_class(key)
        recorded_as = native_attribute_name(converted, key)
        if (
            canonical_attribute_key(key) in _INPUT_HISTORY_KEYS
            or recorded_as in _INPUT_HISTORY_KEYS
            or classify_attribute(recorded_as) is not expected
        ):
            recorder.record_omission(key, capture_class=expected)
            continue
        kept[key] = value
    converted["attributes"] = kept
    return converted


# Which adapter consumes a scope's spans, and the input it takes. A plain mapping:
# Pipecat in Month 1.
_ADAPTERS_BY_SCOPE: dict[str, tuple[type, Callable[[IncidentRecorder, OtlpSpan], Any]]] = (
    dict.fromkeys(PIPECAT_SCOPES, (PipecatAdapter, _pipecat_input))
)


def _add_recording(
    recorder: IncidentRecorder, recording: Recording, domain: str, policy: CapturePolicy
) -> None:
    participant_id, role, stream_id, direction = _SPEAKERS[recording.speaker]
    recorder.add_participant(participant_id, role=role)
    recorder.add_stream(stream_id, participant_id=participant_id, direction=direction)
    started = int(recording.started_at_unix_nano)
    media_domain: str | None = None
    if policy.allows(CaptureClass.AUDIO):
        # The file's own timeline (nanoseconds from its first sample) and where
        # that lies on the call's clock: the client-declared start.
        media_domain = f"media-{recording.recording_id}"
        recorder.register_clock_domain(
            ClockDomain(
                clock_domain_id=media_domain,
                kind="media_timeline",
                observer=MEDIA_CUSTODIAN,
                monotonic_origin_nano="0",
            )
        )
        recorder.register_clock_relation(
            ClockRelation(
                relation_id=f"{media_domain}.to-call",
                from_clock_domain_id=media_domain,
                to_clock_domain_id=domain,
                offset_nano=str(started),
                method="client_declared_start",
            )
        )
    recorder.add_media_ref(
        MediaRef(
            media_id=f"recording-{recording.recording_id}",
            session_id=recorder.session_id,
            stream_id=stream_id,
            media_kind="audio",
            content_type=recording.content_type,
            integrity="content_digest",
            sha256=recording.sha256,
            size_bytes=recording.size,
            custodian=MEDIA_CUSTODIAN,
            clock_domain_id=media_domain,
            time_range=TimeRange(
                start=TimePoint(source_time_unix_nano=str(started), clock_domain_id=domain),
                end=TimePoint(
                    source_time_unix_nano=str(_recording_end(recording)), clock_domain_id=domain
                ),
            ),
        )
    )


def transcript_coverage(lines: Sequence[TranscriptLine], preset: str) -> str:
    """Whether a call's transcript is there: ``available`` (it has lines), else
    ``not_captured_by_preset`` under ``metadata_only``, else ``not_observed``."""

    if lines:
        return TRANSCRIPT_AVAILABLE
    if preset == CAPTURE_PRESET_METADATA_ONLY:
        return TRANSCRIPT_NOT_CAPTURED
    return TRANSCRIPT_NOT_OBSERVED


def _record_transcript_coverage(
    recorder: IncidentRecorder, spans: Sequence[OtlpSpan], preset: str
) -> None:
    """Declare whether the call's spans hold a transcript (``transcript_lines``)."""

    coverage = transcript_coverage(transcript_lines(spans), preset)
    if coverage == TRANSCRIPT_AVAILABLE:
        recorder.record_coverage("call.transcript", "available")
    elif coverage == TRANSCRIPT_NOT_CAPTURED:
        recorder.record_coverage("call.transcript", "unavailable", "not_captured_by_preset")
    else:
        recorder.record_coverage("call.transcript", "not_observed", "no_transcript_spans")
