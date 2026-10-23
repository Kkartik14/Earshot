"""A call's stored evidence as a revision bundle (FR-7), and as transcript lines (AC7.3).

Pure: it reads nothing and writes nothing. The closer (``storage_closer``) takes a
snapshot of a call's spans and stored recordings and hands it here; the receiver
asks here for each span's transcript line as it stores the span.

**Transcript lines** come from one rule, ``transcript_text``. For Pipecat, a user
line is every ``stt`` span with a non-blank ``transcript`` (``is_final`` is not a
filter: it was false on every ``stt`` span of the real 1.5 capture). An agent
line is an ``llm`` span's ``output``, which is complete on every Pipecat
version and keeps partial output on interruption; ``tts.text`` holds only the
last sentence on 1.5 and is never used. A cancelled ``llm`` span has no output
and gives no line. For LiveKit, a user line is a ``user_turn``'s transcript and
an agent line an ``agent_turn``'s reply text, the text actually played (cut where
an interruption stopped it), under either spelling (``lk.pii.*`` from 1.7,
``lk.*`` before): parsed by attribute, never by version. A LiveKit ``llm_request``
output never gives a line, because a preemptive generation LiveKit discarded
still carries one that was never spoken. ``"NOT_GIVEN"`` is an absent value. The
receiver stores each
span's line when it stores the span (``spans.text_json``), so the read API reads
a call's transcript without its spans' payloads; a revision reads it from the
spans it is built from (``transcript_lines``).

Connector transcript-line spans use the same speaker and text vocabulary. Their
``earshot.connector.transcript.timing`` and ``.index`` attributes also survive storage,
so an untimed provider line stays unplaced and same-time lines keep their source order.

**A revision** (``materialize``) is built the way the hosted connectors build a
bundle on the server: an ``IncidentRecorder`` on a ``ManualClock`` set from the
evidence (first start to last end; never the server clock), bundle ID
``<call_id>.r<n>``, session ID ``call_id``, clock domain ``otlp-<call_id>``, and
a manual trace ID derived from the bundle ID, so one snapshot always gives the
same bytes. Spans go in ``(start, trace_id, span_id)`` order to the adapter their
instrumentation scope names (``_ADAPTERS_BY_SCOPE``: Pipecat and LiveKit), as the
dict ``adapter_span`` converts them to; spans of other scopes stay in the catalog
and are declared, not bundled.

What a span carries reaches the bundle only under the capture class the receiver
gave it (``call_capture.attribute_capture_class``): an attribute the recorder
would file under another class, and LLM input history (the whole conversation,
again, on every ``llm`` span), are left out with a declared omission, so the
output and transcript fit the recorder's value budget and the bundle grows
linearly with the call. Full fidelity stays in ``spans``.

Each stored recording becomes a ``MediaRef`` held by ``earshot.blob_store``
(its sha256 and size, its speaker's stream), on its own media clock domain with a
``ClockRelation`` from ``started_at_unix_nano`` to the call's clock. A recording
with a channel map becomes one ``AudioStream`` and one ``MediaRef`` per channel
(``channel_index``), all on the file's one media clock domain. An
end-signal close is ``final``; any other close is a partial observation
(``close_partial``, ``otlp_quiet_period``/``no_end_signal``, ``interrupted``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .adapters.livekit import LiveKitAdapter
from .adapters.pipecat import PipecatAdapter, native_attribute_name
from .call_capture import (
    CAPTURE_PRESET_FULL,
    CAPTURE_PRESET_METADATA_ONLY,
    LIVEKIT_SCOPE,
    NOT_GIVEN,
    OtlpSpan,
    any_value,
    attribute_capture_class,
    attribute_values,
    canonical_attribute_key,
    recorder_agrees,
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
    timing: str = "measured"
    transcript_index: int | None = None


def _span_order(span: OtlpSpan) -> tuple[int, bytes, bytes]:
    return (span.span.start_time_unix_nano, span.span.trace_id, span.span.span_id)


# The span kinds that carry a transcript line, by (instrumentation scope, span name):
# ``(speaker, attributes)``, the first attribute present giving the text. ``None``
# means the speaker is carried by an attribute, as it is for uploaded transcripts.
TRANSCRIPT_SPAN_SOURCES: dict[tuple[str, str], tuple[str | None, tuple[str, ...]]] = {
    **{(scope, "stt"): ("user", ("transcript",)) for scope in PIPECAT_SCOPES},
    **{(scope, "llm"): ("agent", ("output",)) for scope in PIPECAT_SCOPES},
    (LIVEKIT_SCOPE, "user_turn"): ("user", ("lk.pii.user_transcript", "lk.user_transcript")),
    (LIVEKIT_SCOPE, "agent_turn"): ("agent", ("lk.pii.response.text", "lk.response.text")),
}
_CONNECTOR_TRANSCRIPT_SOURCE = (None, ("earshot.connector.transcript.text",))
_CONNECTOR_TRANSCRIPT_SPAN = "earshot.connector.transcript_line"
_CONNECTOR_TRANSCRIPT_TIMING = "earshot.connector.transcript.timing"
_CONNECTOR_TRANSCRIPT_INDEX = "earshot.connector.transcript.index"
_TRANSCRIPT_TIMINGS = frozenset({"measured", "start_only", "order_only"})
_UPLOAD_SCOPE = "earshot.upload"
# ``transcript_coverage`` values: lines found; the preset stores none; none observed.
TRANSCRIPT_AVAILABLE = "available"
TRANSCRIPT_NOT_CAPTURED = "not_captured_by_preset"
TRANSCRIPT_NOT_OBSERVED = "not_observed"


def _line_source(span: OtlpSpan) -> tuple[str | None, tuple[str, ...]] | None:
    """``(speaker, attributes)`` of the span kinds that carry a transcript line."""

    if span.span.name == _CONNECTOR_TRANSCRIPT_SPAN and (
        span.scope.name == _UPLOAD_SCOPE or span.scope.name.startswith("earshot.connector.")
    ):
        return _CONNECTOR_TRANSCRIPT_SOURCE
    return TRANSCRIPT_SPAN_SOURCES.get((span.scope.name, span.span.name))


def _attribute_value(attributes: Iterable[Any], keys: tuple[str, ...]) -> Any:
    """The value of the first of ``keys`` the span carries (``attribute_values``'s
    reading), converting only that value: an ``llm`` span's other attributes include
    the whole conversation (``input``)."""

    found: dict[str, Any] = {}
    for item in attributes:
        if item.key not in keys or item.key in found:
            continue
        value = any_value(item.value)
        if value is not None and value != NOT_GIVEN:
            found[item.key] = value
    return next((found[key] for key in keys if key in found), None)


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
    speaker, attributes = source
    text = _attribute_value(span.span.attributes, attributes)
    if not isinstance(text, str) or not text.strip():
        return None
    if speaker is None:
        uploaded_speaker = _attribute_value(
            span.span.attributes, ("earshot.connector.transcript.speaker",)
        )
        if uploaded_speaker not in {"user", "agent"}:
            return None
        speaker = uploaded_speaker
    return speaker, text


def _transcript_metadata(span: OtlpSpan) -> tuple[str, int | None]:
    """Timing provenance and source order carried by a connector transcript line."""

    if _line_source(span) is None or span.span.name != _CONNECTOR_TRANSCRIPT_SPAN:
        return "measured", None
    timing = _attribute_value(span.span.attributes, (_CONNECTOR_TRANSCRIPT_TIMING,))
    if not isinstance(timing, str) or timing not in _TRANSCRIPT_TIMINGS:
        # Older or malformed connector spans with no usable timing declaration are
        # only considered measured when their span has a non-zero time window.
        timing = (
            "order_only"
            if timing is not None or span.span.start_time_unix_nano == span.span.end_time_unix_nano
            else "measured"
        )
    index = _attribute_value(span.span.attributes, (_CONNECTOR_TRANSCRIPT_INDEX,))
    if type(index) is not int or index < 0:
        index = None
    return timing, index


def transcript_text_json(span: OtlpSpan) -> str | None:
    """The span's line as ``spans.text_json`` stores it (``None``: no line)."""

    line = transcript_text(span)
    if line is None:
        return None
    speaker, text = line
    timing, index = _transcript_metadata(span)
    document: dict[str, Any] = {"speaker": speaker, "text": text}
    if timing != "measured":
        document["timing"] = timing
    if index is not None:
        document["index"] = index
    return json.dumps(document, separators=(",", ":"))


def transcript_text_json_from_stored_span(scope_name: str, payload: bytes) -> str | None:
    """Extract a transcript line from an older stored connector span payload.

    Uploads and connector transcripts were accepted before ``spans.text_json`` knew
    those span kinds. Call detail uses this only for their legacy rows; new spans
    continue to read their precomputed text without parsing payloads.
    """

    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    return transcript_text_json(
        OtlpSpan(
            resource=Resource(),
            scope=InstrumentationScope(name=scope_name),
            span=Span.FromString(payload),
        )
    )


def stored_transcript_line(
    text_json: str, *, start_unix_nano: int, end_unix_nano: int, trace_id: bytes, span_id: bytes
) -> TranscriptLine:
    """A ``spans.text_json`` value and its span's columns as a transcript line."""

    line = json.loads(text_json)
    timing = line.get("timing", "measured")
    if not isinstance(timing, str) or timing not in _TRANSCRIPT_TIMINGS:
        timing = "order_only"
    index = line.get("index")
    if type(index) is not int or index < 0:
        index = None
    return TranscriptLine(
        speaker=line["speaker"],
        text=line["text"],
        start_unix_nano=start_unix_nano,
        end_unix_nano=end_unix_nano,
        trace_id=trace_id.hex(),
        span_id=span_id.hex(),
        timing=timing,
        transcript_index=index,
    )


def transcript_order(
    line: TranscriptLine, *, effective_start_unix_nano: int | None = None
) -> tuple[int, str, int, int, str]:
    """Effective start, trace, explicit source index, then span ID."""

    return (
        line.start_unix_nano if effective_start_unix_nano is None else effective_start_unix_nano,
        line.trace_id,
        0 if line.transcript_index is not None else 1,
        line.transcript_index if line.transcript_index is not None else 0,
        line.span_id,
    )


def ordered_transcript_lines(lines: Iterable[TranscriptLine]) -> list[TranscriptLine]:
    """Order by time while retaining indexed provider source order within a trace.

    An order-only line's span time is the call start. Use the greatest preceding
    indexed time as its effective sort time so it stays between source neighbors
    without giving its public line a fabricated timestamp. The monotonic maximum
    also preserves source order if provider timestamps arrive out of order.
    """

    materialized = list(lines)
    effective_starts: dict[tuple[str, str], int] = {}
    by_trace: dict[str, list[TranscriptLine]] = {}
    for line in materialized:
        if line.transcript_index is not None:
            by_trace.setdefault(line.trace_id, []).append(line)
    for trace_lines in by_trace.values():
        previous_start: int | None = None
        for line in sorted(trace_lines, key=lambda item: (item.transcript_index, item.span_id)):
            start = line.start_unix_nano
            if previous_start is not None:
                start = max(start, previous_start)
            effective_starts[(line.trace_id, line.span_id)] = start
            previous_start = start
    return sorted(
        materialized,
        key=lambda line: transcript_order(
            line,
            effective_start_unix_nano=effective_starts.get((line.trace_id, line.span_id)),
        ),
    )


def transcript_lines(spans: Iterable[OtlpSpan]) -> list[TranscriptLine]:
    """The transcript lines from spans, ordered for the call detail."""

    lines: list[TranscriptLine] = []
    for span in spans:
        line = transcript_text(span)
        if line is None:
            continue
        speaker, text = line
        timing, index = _transcript_metadata(span)
        lines.append(
            TranscriptLine(
                speaker=speaker,
                text=text,
                start_unix_nano=span.span.start_time_unix_nano,
                end_unix_nano=span.span.end_time_unix_nano,
                trace_id=span.span.trace_id.hex(),
                span_id=span.span.span_id.hex(),
                timing=timing,
                transcript_index=index,
            )
        )
    return ordered_transcript_lines(lines)


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


def _livekit_input(recorder: IncidentRecorder, span: OtlpSpan) -> dict[str, Any]:
    """The span as the LiveKit adapter consumes it, keeping only attributes the
    adapter records (under their semantic key, ``call_capture.ATTRIBUTE_KEYS``) in the
    class the receiver gives them, and no LLM input history (the rest declared)."""

    converted = adapter_span(span)
    kept: dict[str, Any] = {}
    for key, value in converted["attributes"].items():
        expected = attribute_capture_class(key)
        recorded_as = canonical_attribute_key(key)
        if recorded_as in _INPUT_HISTORY_KEYS or not recorder_agrees(recorded_as, expected):
            recorder.record_omission(key, capture_class=expected)
            continue
        kept[key] = value
    converted["attributes"] = kept
    return converted


# Which adapter consumes a scope's spans, and the input it takes. A plain mapping.
_ADAPTERS_BY_SCOPE: dict[str, tuple[type, Callable[[IncidentRecorder, OtlpSpan], Any]]] = {
    **dict.fromkeys(PIPECAT_SCOPES, (PipecatAdapter, _pipecat_input)),
    LIVEKIT_SCOPE: (LiveKitAdapter, _livekit_input),
}


def _recording_channels(recording: Recording) -> list[tuple[int | None, str, str]]:
    """``(channel_index, speaker, media_id)`` of each reference a recording becomes: the
    whole file under its speaker, or one per channel of its channel map."""

    if recording.channel_speakers is None:
        return [(None, recording.speaker, f"recording-{recording.recording_id}")]
    return [
        (index, speaker, f"recording-{recording.recording_id}.ch{index}")
        for index, speaker in enumerate(recording.channel_speakers)
    ]


def _add_recording(
    recorder: IncidentRecorder, recording: Recording, domain: str, policy: CapturePolicy
) -> None:
    channels = _recording_channels(recording)
    for _, speaker, _ in channels:
        participant_id, role, stream_id, direction = _SPEAKERS[speaker]
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
    time_range = TimeRange(
        start=TimePoint(source_time_unix_nano=str(started), clock_domain_id=domain),
        end=TimePoint(source_time_unix_nano=str(_recording_end(recording)), clock_domain_id=domain),
    )
    for channel_index, speaker, media_id in channels:
        recorder.add_media_ref(
            MediaRef(
                media_id=media_id,
                session_id=recorder.session_id,
                stream_id=_SPEAKERS[speaker][2],
                channel_index=channel_index,
                media_kind="audio",
                content_type=recording.content_type,
                integrity="content_digest",
                sha256=recording.sha256,
                size_bytes=recording.size,
                custodian=MEDIA_CUSTODIAN,
                clock_domain_id=media_domain,
                time_range=time_range,
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
