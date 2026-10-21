"""A call's projection: its version, and whether a stored one is fresh.

A projection is what a call's latest revision, that revision's audio analyses and
the prices say about the call: the summary columns the call list filters on, and
the detail the call view shows (``call_projections``). The projection job writes
it; the call reads in ``storage_call_reads`` serve it with its freshness.

**Freshness** (``projection_state``) compares a projection's stamps with what it
was computed from:

- ``none``: the call has no projection;
- ``stale``: the call changed since (its ``projection_seq`` moved past the
  projection's ``source_seq``), or the project's or the built-in prices changed,
  or the projection was computed by an older ``CALL_PROJECTION_VERSION``;
- ``current``: none of those.

A stale projection is still served: it is the latest computed view, and the job
replaces it.

**Computation** (``compute_projection``) is a pure function of the call's stored
spans, its stored recordings with their analyses, and the price rows: it reads no
clock, database or environment, so the job runs it outside any lock and a rerun on
the same inputs gives the same projection. It never decodes a revision bundle: the
spans as stored carry everything it reads (``call_span_facts``: usage and turn
anchors; ``call_materializer.transcript_lines``; the agent, outcome and tag
attributes), and the bundle hashes some of them.

- **Agent:** ``earshot.agent.name``, else ``gen_ai.agent.name``, else
  ``lk.agent_name``, else the resource ``service.name`` (``unknown_service…``, the
  SDK default, counts as absent): the first span carrying the key wins.
- **Outcome:** the last ``earshot.call.outcome`` sent, else ``error`` if a span has
  error status, else ``interrupted`` if the call had no end signal, else
  ``completed``.
- **Tags:** the union of ``earshot.call.tags`` (a string array, or one string).
- **Axis:** call times are milliseconds from the earliest stored recording's start,
  else from the earliest span's start.
- **Turns:** one per framework turn (``call_span_facts.turn_anchors``), with the
  framework's reported latency and the audible gap at the agent
  (``call_timeline``). Each turn's spans (its descendants) are listed for the lazy
  span route.
- **Transcript placement:** a line is mapped onto the timeline with its turn's
  offset (the median of the turn's anchor offsets; 0 without anchors). A user line
  snaps to the nearest user run start within ``USER_SNAP_MS``; an agent line to its
  turn's paired agent run. A snapped line ends where the last run of its speaker
  that starts before the next line ends. Without a timeline, lines keep their span
  times. A line of a turn with a timeline discontinuity is ``alignment_uncertain``.
- **Cost:** ``call_cost``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .call_capture import (
    LIVEKIT_SCOPE,
    NOT_GIVEN,
    any_value,
    is_tail_span,
)
from .call_cost import CallCost, PriceRow, call_usage, price_usage
from .call_materializer import PIPECAT_SCOPES, transcript_lines
from .call_span_facts import turn_anchors, usage_items
from .call_timeline import (
    USER_SNAP_MS,
    Gap,
    Run,
    Signals,
    Timeline,
    TurnAnchorsMs,
    audible_gap,
    not_measurable,
    signals,
    talk_time,
)

if TYPE_CHECKING:
    from .call_capture import OtlpSpan

# Bumped when the projection's computation or shape changes: every stored
# projection then reads as stale until the job recomputes it.
CALL_PROJECTION_VERSION = 1
PROJECTION_STATES = ("none", "stale", "current")


@dataclass(frozen=True, slots=True)
class ProjectionStamps:
    """The versions a projection was computed from (``call_projections``)."""

    source_seq: int
    price_version: int
    builtin_price_version: int
    projection_version: int


@dataclass(frozen=True, slots=True)
class ProjectionInputs:
    """The versions a projection would be computed from now."""

    projection_seq: int
    price_version: int
    builtin_price_version: int


def projection_state(stamps: ProjectionStamps | None, inputs: ProjectionInputs) -> str:
    """``none``, ``stale`` or ``current`` (module docstring)."""

    if stamps is None:
        return "none"
    if (
        stamps.source_seq < inputs.projection_seq
        or stamps.price_version < inputs.price_version
        or stamps.builtin_price_version < inputs.builtin_price_version
        or stamps.projection_version < CALL_PROJECTION_VERSION
    ):
        return "stale"
    return "current"


# --- computation --------------------------------------------------------------------------

AGENT_KEYS = ("earshot.agent.name", "gen_ai.agent.name", "lk.agent_name")
OUTCOME_KEY = "earshot.call.outcome"
TAGS_KEY = "earshot.call.tags"
LABEL_MAX_LENGTH = 256
MAX_TAGS = 64
_NANOS_PER_MS = 1_000_000
_ROLES = {
    "stt": "stt",
    "llm": "llm",
    "tts": "tts",
    "llm_request": "llm",
    "tts_request": "tts",
    "function_tool": "tool",
}
_STATUS_NAMES = {0: "unset", 1: "ok", 2: "error"}


@dataclass(frozen=True, slots=True)
class RecordingSource:
    """One stored recording of the call, with its analysis (``None``: no row)."""

    recording_id: str
    speaker: str
    channel_speakers: tuple[str, ...] | None
    content_type: str
    started_at_unix_nano: int
    duration_ms: int | None
    analysis_state: str | None
    analysis_error_code: str | None
    # ``audio_analysis.result_json`` of a stored analysis.
    result: Mapping[str, Any] | None
    has_peaks: bool
    holds_rendition: bool

    @property
    def speakers(self) -> tuple[str, ...]:
        return self.channel_speakers or (self.speaker,)


@dataclass(frozen=True, slots=True)
class CallSource:
    """What one call's projection is computed from."""

    project_id: str
    call_id: str
    revision: int
    first_evidence_unix_nano: int
    ended: bool
    spans: tuple[OtlpSpan, ...]
    recordings: tuple[RecordingSource, ...]


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    """One ``call_transcripts`` row."""

    line_no: int
    speaker: str
    timing: str
    start_ms: int | None
    end_ms: int | None
    text: str


@dataclass(frozen=True, slots=True)
class Projection:
    """A computed projection: its summary columns (every ``call_projections`` column
    but the stamps), tags, transcript rows, usage and detail."""

    columns: dict[str, Any]
    tags: tuple[str, ...]
    transcript: tuple[TranscriptRow, ...]
    usage: list[dict[str, Any]]
    detail: dict[str, Any]

    def input_digest(self) -> str:
        """sha256 (hex) of everything it holds: an equal digest is an equal projection."""

        document = {
            "projection_version": CALL_PROJECTION_VERSION,
            "columns": self.columns,
            "tags": self.tags,
            "transcript": [_row(row) for row in self.transcript],
            "usage": self.usage,
            "detail": self.detail,
        }
        return hashlib.sha256(canonical_json(document).encode()).hexdigest()


def _row(row: TranscriptRow) -> list[Any]:
    return [row.line_no, row.speaker, row.timing, row.start_ms, row.end_ms, row.text]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _attribute(attributes: Any, key: str) -> Any:
    for item in attributes:
        if item.key == key:
            value = any_value(item.value)
            if value is not None and value != NOT_GIVEN:
                return value
    return None


def _label(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= LABEL_MAX_LENGTH:
        return value
    return None


def call_agent(spans: Sequence[OtlpSpan]) -> str | None:
    for key in AGENT_KEYS:
        for span in spans:
            found = _label(_attribute(span.span.attributes, key))
            if found is not None:
                return found
    for span in spans:
        name = _label(_attribute(span.resource.attributes, "service.name"))
        if name is not None and not name.startswith("unknown_service"):
            return name
    return None


def call_outcome(spans: Sequence[OtlpSpan], *, ended: bool) -> str:
    explicit = [
        found
        for span in spans
        if (found := _label(_attribute(span.span.attributes, OUTCOME_KEY))) is not None
    ]
    if explicit:
        return explicit[-1]
    if any(span.span.status.code == 2 for span in spans):
        return "error"
    return "completed" if ended else "interrupted"


def call_tags(spans: Sequence[OtlpSpan]) -> tuple[str, ...]:
    tags: set[str] = set()
    for span in spans:
        for attributes in (span.span.attributes, span.resource.attributes):
            value = _attribute(attributes, TAGS_KEY)
            values = value if isinstance(value, list) else [value]
            tags.update(tag for tag in values if _label(tag) is not None)
    return tuple(sorted(tags)[:MAX_TAGS])


def call_framework(spans: Sequence[OtlpSpan]) -> str | None:
    scopes = {span.scope.name for span in spans}
    if scopes & PIPECAT_SCOPES:
        return "pipecat"
    if LIVEKIT_SCOPE in scopes:
        return "livekit"
    return None


def _p50(values: Sequence[int]) -> int | None:
    """The nearest-rank median: an observed value, the lower middle of an even count."""

    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) / 2) - 1]


def _runs(channel: Mapping[str, Any]) -> tuple[Run, ...]:
    return tuple(
        Run(
            start_ms=int(run["start_ms"]),
            end_ms=int(run["end_ms"]),
            start_sigma_ms=int(run["start_sigma_ms"]),
            end_sigma_ms=int(run["end_sigma_ms"]),
        )
        for run in channel.get("runs", ())
    )


def _channel(result: Mapping[str, Any] | None, speaker: str) -> Mapping[str, Any] | None:
    if result is None:
        return None
    return next(
        (channel for channel in result.get("channels", ()) if channel.get("speaker") == speaker),
        None,
    )


def _stored(recording: RecordingSource) -> bool:
    return recording.analysis_state == "stored" and recording.result is not None


@dataclass(frozen=True, slots=True)
class _TimelineChoice:
    timeline: Timeline | None
    # Why there is none: no_recording, analysis_not_stored or not_one_timeline.
    reason: str | None
    # The user audio's duration (ms), for the STT estimate.
    user_audio_ms: int | None


def _pairs(recordings: Sequence[RecordingSource]) -> list[tuple[RecordingSource, RecordingSource]]:
    users = [r for r in recordings if r.speakers == ("user",)]
    agents = [r for r in recordings if r.speakers == ("agent",)]
    pairs = []
    for user in users:
        partners = [
            agent
            for agent in agents
            if agent.started_at_unix_nano == user.started_at_unix_nano
            and user.duration_ms is not None
            and agent.duration_ms is not None
            and abs(agent.duration_ms - user.duration_ms) <= _ONE_TIMELINE_TOLERANCE_MS
        ]
        if len(partners) == 1:
            pairs.append((user, partners[0]))
    return pairs


_ONE_TIMELINE_TOLERANCE_MS = 20


def choose_timeline(recordings: Sequence[RecordingSource], *, insert_only: bool) -> _TimelineChoice:
    """The call's recorder timeline: a stereo user/agent recording, else a mono pair."""

    stereo = [r for r in recordings if set(r.speakers) == {"user", "agent"}]
    pairs = _pairs(recordings)
    users = [r for r in recordings if "user" in r.speakers]
    user_audio_ms = max((r.duration_ms for r in users if r.duration_ms is not None), default=None)
    for recording in stereo:
        user, agent = _channel(recording.result, "user"), _channel(recording.result, "agent")
        if _stored(recording) and user is not None and agent is not None:
            duration = recording.result.get("source", {}).get("duration_ms")
            return _TimelineChoice(
                Timeline(
                    started_at_unix_nano=recording.started_at_unix_nano,
                    duration_ms=recording.duration_ms if duration is None else int(duration),
                    user_runs=_runs(user),
                    agent_runs=_runs(agent),
                    agent_zero_runs=_zero_runs(agent),
                    insert_only=insert_only,
                ),
                None,
                recording.duration_ms if recording.duration_ms is not None else duration,
            )
    for user_recording, agent_recording in pairs:
        user = _channel(user_recording.result, "user")
        agent = _channel(agent_recording.result, "agent")
        if (
            _stored(user_recording)
            and _stored(agent_recording)
            and user is not None
            and agent is not None
        ):
            return _TimelineChoice(
                Timeline(
                    started_at_unix_nano=user_recording.started_at_unix_nano,
                    duration_ms=user_recording.duration_ms,
                    user_runs=_runs(user),
                    agent_runs=_runs(agent),
                    agent_zero_runs=_zero_runs(agent),
                    insert_only=insert_only,
                ),
                None,
                user_recording.duration_ms,
            )
    if not recordings:
        reason = "no_recording"
    elif stereo or pairs:
        reason = "analysis_not_stored"
    else:
        reason = "not_one_timeline"
    return _TimelineChoice(None, reason, user_audio_ms)


def _zero_runs(channel: Mapping[str, Any]) -> tuple[tuple[int, int], ...]:
    return tuple((int(run["start_ms"]), int(run["end_ms"])) for run in channel.get("zero_runs", ()))


def _call_analysis_state(recordings: Sequence[RecordingSource]) -> str:
    states = [r.analysis_state or "pending" for r in recordings]
    if not states:
        return "none"
    if any(state in ("pending", "running") for state in states):
        return "pending"
    if all(state == "stored" for state in states):
        return "stored"
    if "stored" in states:
        return "partial"
    if "failed" in states:
        return "failed"
    return "unavailable"


def _media(recordings: Sequence[RecordingSource], axis_ms: Any) -> list[dict[str, Any]]:
    entries = []
    paired: set[str] = set()
    for user, agent in _pairs(recordings):
        if not user.holds_rendition:
            continue
        paired.update((user.recording_id, agent.recording_id))
        states = [(r.analysis_state or "pending", r.analysis_error_code) for r in (user, agent)]
        state, error = next(
            ((state, error) for state, error in states if state != "stored"), ("stored", None)
        )
        base = f"/v1/recordings/{user.recording_id}/rendition"
        entries.append(
            {
                "source": "rendition",
                "recording_ids": [user.recording_id, agent.recording_id],
                "content_path": base,
                "content_type": "audio/flac",
                "peaks_path": f"{base}/peaks" if user.has_peaks and agent.has_peaks else None,
                "offset_ms": axis_ms(user.started_at_unix_nano),
                "duration_ms": user.duration_ms,
                "channels": [{"index": 0, "speaker": "user"}, {"index": 1, "speaker": "agent"}],
                "analysis_state": state,
                "analysis_error_code": error,
            }
        )
    for recording in recordings:
        if recording.recording_id in paired:
            continue
        base = f"/v1/recordings/{recording.recording_id}"
        entries.append(
            {
                "source": "recording",
                "recording_ids": [recording.recording_id],
                "content_path": f"{base}/content",
                "content_type": recording.content_type,
                "peaks_path": f"{base}/peaks" if recording.has_peaks else None,
                "offset_ms": axis_ms(recording.started_at_unix_nano),
                "duration_ms": recording.duration_ms,
                "channels": [
                    {"index": index, "speaker": speaker}
                    for index, speaker in enumerate(recording.speakers)
                ],
                "analysis_state": recording.analysis_state or "pending",
                "analysis_error_code": recording.analysis_error_code,
            }
        )
    return sorted(entries, key=lambda entry: (entry["offset_ms"], entry["recording_ids"]))


def _descendants(spans: Sequence[OtlpSpan], trace: bytes, root: bytes) -> list[OtlpSpan]:
    children: dict[bytes, list[OtlpSpan]] = {}
    for span in spans:
        if span.span.trace_id == trace and span.span.parent_span_id:
            children.setdefault(span.span.parent_span_id, []).append(span)
    found, stack = [], [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child.span.span_id)
    return sorted(found, key=lambda span: (span.span.start_time_unix_nano, span.span.span_id))


def _turn_id(turn: OtlpSpan | None) -> str | None:
    if turn is None:
        return None
    for key in ("earshot.turn.id", "turn.id", "turn.number", "lk.speech_id"):
        value = _attribute(turn.span.attributes, key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def compute_projection(source: CallSource, prices: Sequence[PriceRow]) -> Projection:
    """The call's projection (module docstring)."""

    spans = sorted(
        source.spans,
        key=lambda span: (span.span.start_time_unix_nano, span.span.trace_id, span.span.span_id),
    )
    evidence = [span for span in spans if not is_tail_span(span)]
    framework = call_framework(spans)
    recordings = sorted(source.recordings, key=lambda r: (r.started_at_unix_nano, r.recording_id))
    if recordings:
        origin = min(r.started_at_unix_nano for r in recordings)
        axis_origin = "recording_start"
    elif evidence:
        origin = evidence[0].span.start_time_unix_nano
        axis_origin = "first_evidence"
    else:
        origin = source.first_evidence_unix_nano
        axis_origin = "first_evidence"

    def axis_ms(unix_nano: int | float) -> int:
        return round((unix_nano - origin) / _NANOS_PER_MS)

    choice = choose_timeline(recordings, insert_only=framework == "pipecat")
    timeline = choice.timeline
    timeline_shift = 0.0 if timeline is None else (timeline.started_at_unix_nano - origin) / 1e6

    def placed_ms(unix_nano: int, offset: float) -> int:
        """A span time on the axis, through the timeline with a turn's offset."""

        if timeline is None:
            return axis_ms(unix_nano)
        return round(timeline.naive_ms(unix_nano) + offset + timeline_shift)

    def timeline_axis(value: int | None) -> int | None:
        return None if value is None else round(value + timeline_shift)

    by_id = {(span.span.trace_id.hex(), span.span.span_id.hex()): span for span in spans}
    anchors = turn_anchors(spans)
    gaps: list[Gap] = []
    turns: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors):
        gap = (
            audible_gap(
                timeline,
                TurnAnchorsMs(
                    anchor.user_on_unix_nano,
                    anchor.user_off_unix_nano,
                    anchor.agent_on_unix_nano,
                ),
            )
            if timeline is not None
            else not_measurable(choice.reason or "no_recording")
        )
        gaps.append(gap)
        turn_span = by_id.get((anchor.trace_id, anchor.turn_span_id))
        children = (
            []
            if turn_span is None
            else _descendants(spans, turn_span.span.trace_id, turn_span.span.span_id)
        )
        turns.append(
            {
                "index": index,
                "turn_id": _turn_id(turn_span),
                "start_ms": None
                if turn_span is None
                else placed_ms(turn_span.span.start_time_unix_nano, gap.offset_ms),
                "end_ms": None
                if turn_span is None
                else placed_ms(turn_span.span.end_time_unix_nano, gap.offset_ms),
                "reported_latency_ms": None
                if anchor.reported_latency_ms is None
                else round(anchor.reported_latency_ms),
                "audible_gap": {
                    "status": gap.status,
                    "value_ms": gap.value_ms,
                    "uncertainty_ms": gap.uncertainty_ms,
                    "bound_ms": gap.bound_ms,
                    "user_end_ms": timeline_axis(gap.user_end_ms),
                    "agent_start_ms": timeline_axis(gap.agent_start_ms),
                    "reason": gap.reason,
                    "basis": gap.basis,
                },
                "spans": [
                    {
                        "trace_id": child.span.trace_id.hex(),
                        "span_id": child.span.span_id.hex(),
                        "name": child.span.name,
                        "role": _ROLES.get(child.span.name, "other"),
                        "status": _STATUS_NAMES.get(child.span.status.code, "unset"),
                        "start_ms": axis_ms(child.span.start_time_unix_nano),
                        "end_ms": axis_ms(child.span.end_time_unix_nano),
                    }
                    for child in children
                    if child.span.name in _ROLES or child.span.status.code == 2
                ],
            }
        )

    transcript = _place_transcript(spans, anchors, gaps, timeline, axis_ms, timeline_shift)

    if timeline is not None:
        measured = signals(timeline)
        one_timeline: bool | None = True
    else:
        analysed = [r for r in recordings if _stored(r)]
        one_timeline = False if analysed else None

        def channel_talk(speaker: str) -> int | None:
            channels = [_channel(r.result, speaker) for r in analysed]
            found = [channel for channel in channels if channel is not None]
            return sum(talk_time(_runs(channel)) for channel in found) if found else None

        measured = Signals(
            talk_time_user_ms=channel_talk("user"), talk_time_agent_ms=channel_talk("agent")
        )

    evidence_starts = [span.span.start_time_unix_nano for span in evidence]
    evidence_ends = [span.span.end_time_unix_nano for span in evidence]
    for recording in recordings:
        evidence_starts.append(recording.started_at_unix_nano)
        if recording.duration_ms is not None:
            evidence_ends.append(
                recording.started_at_unix_nano + recording.duration_ms * _NANOS_PER_MS
            )
    started = min(evidence_starts, default=None)
    duration_ms = (
        None
        if started is None or not evidence_ends
        else max(0, round((max(evidence_ends) - started) / _NANOS_PER_MS))
    )
    user_audio_ms = choice.user_audio_ms if choice.user_audio_ms is not None else duration_ms
    usage = call_usage(usage_items(evidence), user_audio_ms=user_audio_ms)
    cost = price_usage(
        usage, prices, project_id=source.project_id, at_unix_nano=source.first_evidence_unix_nano
    )
    reported = [
        turn["reported_latency_ms"] for turn in turns if turn["reported_latency_ms"] is not None
    ]
    audible = [
        gap.value_ms for gap in gaps if gap.status == "measured" and gap.value_ms is not None
    ]
    columns = {
        "revision": source.revision,
        "agent": call_agent(spans),
        "framework": framework,
        "outcome": call_outcome(spans, ended=source.ended),
        "started_unix_nano": started,
        "duration_ms": duration_ms,
        "turn_count": len(turns),
        "reported_latency_p50_ms": _p50(reported),
        "reported_latency_max_ms": max(reported, default=None),
        "measurable_turns": len(audible),
        "audible_gap_p50_ms": _p50(audible),
        "audible_gap_max_ms": max(audible, default=None),
        "talk_time_user_ms": measured.talk_time_user_ms,
        "talk_time_agent_ms": measured.talk_time_agent_ms,
        "overlap_ms": measured.overlap_ms,
        "barge_in_count": measured.barge_in_count,
        "talk_over_count": measured.talk_over_count,
        "long_silence_count": measured.long_silence_count,
        "dead_air_ms": measured.dead_air_ms,
        **cost_columns(cost),
        "provider_cost_nano_usd": None,
        "provider_cost_basis": None,
        "analysis_state": _call_analysis_state(recordings),
    }
    detail = {
        "axis_origin_unix_nano": str(origin),
        "axis_origin": axis_origin,
        "media": _media(recordings, axis_ms),
        "turns": turns,
        "signals": {
            "one_timeline": one_timeline,
            "talk_time_user_ms": measured.talk_time_user_ms,
            "talk_time_agent_ms": measured.talk_time_agent_ms,
            "overlap_ms": measured.overlap_ms,
            "barge_in_count": measured.barge_in_count,
            "talk_over_count": measured.talk_over_count,
            "long_silence_count": measured.long_silence_count,
            "dead_air_ms": measured.dead_air_ms,
        },
        "cost_items": list(cost.items),
        "transcript": [
            {
                "line_no": line["line_no"],
                "speaker": line["speaker"],
                "text": line["text"],
                "timing": "measured",
                "start_ms": line["start_ms"],
                "end_ms": line["end_ms"],
                "alignment_uncertain": line["alignment_uncertain"],
                "span": line["span"],
            }
            for line in transcript
        ],
    }
    return Projection(
        columns=columns,
        tags=call_tags(spans),
        transcript=tuple(
            TranscriptRow(
                line_no=line["line_no"],
                speaker=line["speaker"],
                timing="measured",
                start_ms=line["start_ms"],
                end_ms=line["end_ms"],
                text=line["text"],
            )
            for line in transcript
        ),
        usage=usage,
        detail=detail,
    )


def cost_columns(cost: CallCost) -> dict[str, Any]:
    return {
        "cost_lower_bound_nano_usd": cost.lower_bound_nano_usd,
        "cost_estimated_nano_usd": cost.estimated_nano_usd,
        "cost_unknown_items": cost.unknown_items,
    }


def reprice(
    usage: Sequence[Mapping[str, Any]],
    detail: Mapping[str, Any],
    prices: Sequence[PriceRow],
    *,
    project_id: str,
    at_unix_nano: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A stored projection's cost columns and detail at new prices, from its usage
    alone (no span or bundle is read)."""

    cost = price_usage(usage, prices, project_id=project_id, at_unix_nano=at_unix_nano)
    return cost_columns(cost), {**detail, "cost_items": list(cost.items)}


def _place_transcript(
    spans: Sequence[OtlpSpan],
    anchors: Sequence[Any],
    gaps: Sequence[Gap],
    timeline: Timeline | None,
    axis_ms: Any,
    timeline_shift: float,
) -> list[dict[str, Any]]:
    """The call's transcript lines on the axis (module docstring)."""

    lines = transcript_lines(spans)
    parents = {
        (span.span.trace_id.hex(), span.span.span_id.hex()): span.span.parent_span_id.hex()
        for span in spans
    }
    turn_of_span = {(a.trace_id, a.turn_span_id): index for index, a in enumerate(anchors)}

    def turn_index(line: Any) -> int | None:
        key = (line.trace_id, line.span_id)
        if key in turn_of_span:  # a LiveKit agent line is its agent_turn
            return turn_of_span[key]
        parent = (line.trace_id, parents.get(key, ""))
        if parent in turn_of_span:  # a Pipecat line's span is a child of its turn
            return turn_of_span[parent]
        earlier = [
            i for i, a in enumerate(anchors) if a.turn_start_unix_nano <= line.start_unix_nano
        ]
        return earlier[-1] if earlier else (0 if anchors else None)

    placed = []
    used_agent_runs: set[Run] = set()
    for line_no, line in enumerate(lines):
        index = turn_index(line)
        gap = gaps[index] if index is not None else None
        uncertain = gap is not None and gap.reason == "recorder_timeline_discontinuity"
        span = {"trace_id": line.trace_id, "span_id": line.span_id}
        if timeline is None:
            placed.append(
                {
                    "line_no": line_no,
                    "speaker": line.speaker,
                    "text": line.text,
                    "start_ms": axis_ms(line.start_unix_nano),
                    "end_ms": axis_ms(line.end_unix_nano),
                    "snapped": False,
                    "alignment_uncertain": uncertain,
                    "span": span,
                }
            )
            continue
        offset = 0.0 if gap is None else gap.offset_ms
        start = timeline.naive_ms(line.start_unix_nano) + offset
        end = timeline.naive_ms(line.end_unix_nano) + offset
        anchor = anchors[index] if index is not None else None
        snapped = None
        if line.speaker == "user":
            runs = [run.start_ms for run in timeline.user_runs]
            nearest = min(runs, key=lambda point: (abs(point - start), point), default=None)
            if nearest is not None and abs(nearest - start) <= USER_SNAP_MS:
                snapped = nearest
        else:
            if (
                anchor is not None
                and (anchor.trace_id, anchor.turn_span_id) == (line.trace_id, line.span_id)
                and anchor.agent_on_unix_nano is not None
            ):
                start = timeline.naive_ms(anchor.agent_on_unix_nano) + offset
            if (
                gap is not None
                and gap.agent_run is not None
                and gap.agent_run not in used_agent_runs
            ):
                snapped = gap.agent_run.start_ms
                used_agent_runs.add(gap.agent_run)
            else:
                runs = [run.start_ms for run in timeline.agent_runs]
                nearest = min(runs, key=lambda point: (abs(point - start), point), default=None)
                if nearest is not None and abs(nearest - start) <= USER_SNAP_MS:
                    snapped = nearest
        placed.append(
            {
                "line_no": line_no,
                "speaker": line.speaker,
                "text": line.text,
                "start_ms": snapped if snapped is not None else start,
                "end_ms": end,
                "snapped": snapped is not None,
                "alignment_uncertain": uncertain,
                "span": span,
            }
        )
    if timeline is None:
        return [_public(line) for line in placed]
    for position, line in enumerate(placed):
        if not line["snapped"]:
            continue
        following = [
            other["start_ms"]
            for other in placed[position + 1 :]
            if other["start_ms"] > line["start_ms"]
        ]
        limit = min(following, default=math.inf)
        runs = timeline.user_runs if line["speaker"] == "user" else timeline.agent_runs
        own = [run for run in runs if line["start_ms"] <= run.start_ms < limit]
        if own:
            line["end_ms"] = max(run.end_ms for run in own)
    for line in placed:
        line["start_ms"] = round(line["start_ms"] + timeline_shift)
        line["end_ms"] = round(line["end_ms"] + timeline_shift)
    return [_public(line) for line in placed]


def _public(line: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in line.items() if key != "snapped"}
