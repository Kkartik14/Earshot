"""Transcripts a client uploads for a recording: WebVTT or plain text, as lines.

**Formats.** The request's media type names the format, and the body is UTF-8 (a
byte order mark is allowed; another ``charset`` is refused, never guessed):

- ``text/vtt`` is WebVTT. Each cue with text is one line, and its cue times are
  offsets from the recording's ``started_at``: the line's timing is ``measured``.
  ``NOTE``, ``STYLE`` and ``REGION`` blocks, cue identifiers and cue settings are
  read past; markup is removed and character references are decoded.
- ``text/plain`` is one line per non-blank line, with no times: the timing is
  ``order_only`` (the lines' order is all the file says).

**Speakers.** Every line is ``user`` or ``agent``. A WebVTT voice (``<v user>``,
``<v Agent>``) or a ``user:``/``agent:`` prefix names it, ignoring case. A line that
names no speaker is the recording's own speaker when the recording is one speaker's
track; on a mixed recording, such a line is an error. Whitespace inside a line is
collapsed to single spaces, so a re-saved file (other line endings or wrapping) is
the same transcript.

**Bounds.** A body over ``MAX_TRANSCRIPT_BYTES`` is refused by the API before it is
parsed; more than ``MAX_TRANSCRIPT_SOURCE_LINES`` physical rows or
``MAX_TRANSCRIPT_LINES`` transcript lines, a control character, or a time past the
latest 64-bit nanosecond time is refused here. Errors name lines and rules, never the
text.

**Evidence** (``transcript_spans``). The lines are written as transcript-line spans
in the connector vocabulary (span ``earshot.connector.transcript_line``, its
``earshot.connector.transcript.*`` attributes, so one reader serves both), under the
``earshot.upload`` instrumentation scope: children of one ``earshot.upload.transcript``
span carrying the transcript's format, line count and digest. The trace is a
function of the recording's call and the recording, and a span's ID of its kind and
line index, so the same transcript of the same recording is the same spans, and two
recordings of one call each keep their own transcript.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .connectors import evidence
from .recordings import MAX_UNIX_NANO

if TYPE_CHECKING:
    from .call_capture import OtlpSpan

# The largest body the API reads (1 MiB holds hours of conversation as text).
MAX_TRANSCRIPT_BYTES = 1024 * 1024
# The most lines one transcript holds: each is a span written in one transaction.
MAX_TRANSCRIPT_LINES = 10_000
# Leave room for WebVTT headers, cue IDs, blank separators and multiline cues, while
# bounding the raw rows materialized before cue parsing.
MAX_TRANSCRIPT_SOURCE_LINES = MAX_TRANSCRIPT_LINES * 8
# A longer hour field cannot be useful to a recording and would make decimal
# conversion depend on Python's configurable integer-string limit.
MAX_TIMESTAMP_HOUR_DIGITS = 1_000

FORMAT_VTT = "vtt"
FORMAT_TEXT = "text"
# The media types the API accepts, and the format each names.
MEDIA_TYPES = {"text/vtt": FORMAT_VTT, "text/plain": FORMAT_TEXT}
_TIMINGS = {FORMAT_VTT: "measured", FORMAT_TEXT: "order_only"}
_SPEAKERS = frozenset(evidence.SPEAKERS)

# --- Evidence vocabulary ------------------------------------------------------------------

UPLOAD_SCOPE = "earshot.upload"
# The transcript as a whole: the parent of its lines.
TRANSCRIPT_SPAN = "earshot.upload.transcript"
UPLOAD_FORMAT = "earshot.upload.format"
UPLOAD_LINE_COUNT = "earshot.upload.line_count"
UPLOAD_SHA256 = "earshot.upload.sha256"

_TRACE_NAMESPACE = b"earshot.upload.trace.v1\x00"
_SPAN_NAMESPACE = b"earshot.upload.span.v1\x00"
_DIGEST_VERSION = 1
_SPAN_KIND_INTERNAL = 1
_NANOS_PER_MILLISECOND = 1_000_000

_NEWLINE = re.compile(r"\r\n|\r|\n")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WEBVTT = re.compile(r"WEBVTT(?:[ \t].*)?")
_TIMESTAMP = re.compile(r"(?:(\d{2,}):)?([0-5]\d):([0-5]\d)\.(\d{3})")
_SKIPPED_BLOCK = re.compile(r"(?:NOTE|STYLE|REGION)(?:[ \t].*)?")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?(?:[ \t]+([^>]*))?>")
_TAG = re.compile(r"<[^>]*>")
_LABEL = re.compile(r"(user|agent)[ \t]*:[ \t]*(.*)", re.IGNORECASE | re.DOTALL)


class TranscriptFormatError(ValueError):
    """The body is not a transcript this module reads; the message says where."""


@dataclass(frozen=True, slots=True)
class UploadedLine:
    """One line: who spoke, what, and (WebVTT) when, in ms from the recording's start."""

    speaker: str
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Transcript:
    """A parsed transcript: its format and its lines, in file order."""

    format: str
    lines: tuple[UploadedLine, ...]

    @property
    def timing(self) -> str:
        return _TIMINGS[self.format]

    def digest(self) -> str:
        """sha256 (hex) of the format and the lines: what "the same transcript" means."""

        canonical = {
            "v": _DIGEST_VERSION,
            "format": self.format,
            "lines": [[line.speaker, line.text, line.start_ms, line.end_ms] for line in self.lines],
        }
        encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def transcript_format(content_type: str) -> str | None:
    """The format a ``Content-Type`` names (``vtt`` or ``text``), or ``None`` for any
    other media type, or a ``charset`` other than UTF-8."""

    media_type, _, parameters = content_type.partition(";")
    found = MEDIA_TYPES.get(media_type.strip().lower())
    if found is None:
        return None
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset" and value.strip().strip('"').lower() not in {
            "utf-8",
            "utf8",
        }:
            return None
    return found


def parse_transcript(
    body: bytes, transcript_format: str, *, default_speaker: str | None
) -> Transcript:
    """The transcript ``body`` holds, in ``transcript_format`` (``vtt`` or ``text``).

    ``default_speaker`` is the speaker of a line that names none: the recording's
    speaker when it is one speaker's track, else ``None`` (every line must name one).
    """

    if transcript_format not in _TIMINGS:
        raise ValueError(f"unknown transcript format {transcript_format!r}")
    if default_speaker is not None and default_speaker not in _SPEAKERS:
        raise ValueError(f"a line's speaker is user or agent, not {default_speaker!r}")
    rows = _decoded_lines(body)
    if transcript_format == FORMAT_VTT:
        parsed = _vtt_lines(rows, default_speaker)
    else:
        parsed = _text_lines(rows, default_speaker)
    if not parsed:
        raise TranscriptFormatError("the file has no transcript lines")
    return Transcript(transcript_format, tuple(parsed))


def _decoded_lines(body: bytes) -> list[str]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        line = body[: error.start].count(b"\n") + 1
        raise TranscriptFormatError(f"line {line}: the transcript is not UTF-8") from None
    text = text.removeprefix("﻿")
    source_lines = text.count("\n") + text.count("\r") - text.count("\r\n") + 1
    if source_lines > MAX_TRANSCRIPT_SOURCE_LINES:
        raise TranscriptFormatError(
            f"the transcript has more than {MAX_TRANSCRIPT_SOURCE_LINES} source lines"
        )
    rows = _NEWLINE.split(text)
    for number, row in enumerate(rows, start=1):
        if _CONTROL.search(row):
            raise TranscriptFormatError(f"line {number}: control characters are not allowed")
    return rows


def _collapsed(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _append(lines: list[UploadedLine], line: UploadedLine) -> None:
    if len(lines) == MAX_TRANSCRIPT_LINES:
        raise TranscriptFormatError(f"the transcript has more than {MAX_TRANSCRIPT_LINES} lines")
    lines.append(line)


def _text_lines(rows: list[str], default_speaker: str | None) -> list[UploadedLine]:
    lines: list[UploadedLine] = []
    for number, row in enumerate(rows, start=1):
        text = _collapsed(row)
        if not text:
            continue
        labelled = _LABEL.fullmatch(text)
        if labelled is not None:
            speaker, text = labelled.group(1).lower(), labelled.group(2)
            if not text:
                raise TranscriptFormatError(
                    f"line {number}: the line has no text after its speaker"
                )
        elif default_speaker is not None:
            speaker = default_speaker
        else:
            raise TranscriptFormatError(
                f"line {number}: start the line with 'user:' or 'agent:' "
                "(the recording holds both speakers)"
            )
        _append(lines, UploadedLine(speaker, text))
    return lines


def _milliseconds(value: str, number: int) -> int:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise TranscriptFormatError(
            f"line {number}: a cue time is HH:MM:SS.mmm or MM:SS.mmm (minutes and seconds "
            "below 60, three digits of milliseconds)"
        )
    hours, minutes, seconds, millis = match.groups()
    significant_hours = (hours or "0").lstrip("0") or "0"
    if len(significant_hours) > MAX_TIMESTAMP_HOUR_DIGITS:
        raise TranscriptFormatError(
            f"line {number}: the cue time is later than the latest time Earshot stores "
            "(2^63 - 1 ns after 1970)"
        )
    try:
        hour_count = int(significant_hours)
    except ValueError:
        raise TranscriptFormatError(
            f"line {number}: the cue time is later than the latest time Earshot stores "
            "(2^63 - 1 ns after 1970)"
        ) from None
    return ((hour_count * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def _cue_line(
    payload: list[str], start_ms: int, end_ms: int, number: int, default_speaker: str | None
) -> UploadedLine | None:
    """The line a cue's payload says, or ``None`` when it has no text."""

    raw = " ".join(payload)
    voice = _VOICE.search(raw)
    named = voice.group(1).strip() if voice is not None and voice.group(1) else None
    text = _collapsed(html.unescape(_TAG.sub("", raw)))
    if not text:
        return None
    if named is not None:
        speaker = named.lower()
        if speaker not in _SPEAKERS:
            raise TranscriptFormatError(
                f"line {number}: the speaker must be user or agent (<v user>, <v agent>)"
            )
    elif (labelled := _LABEL.fullmatch(text)) is not None:
        speaker, text = labelled.group(1).lower(), labelled.group(2)
        if not text:
            return None
    elif default_speaker is not None:
        speaker = default_speaker
    else:
        raise TranscriptFormatError(
            f"line {number}: name the speaker with <v user> or <v agent> "
            "(the recording holds both speakers)"
        )
    return UploadedLine(speaker, text, start_ms, end_ms)


def _vtt_lines(rows: list[str], default_speaker: str | None) -> list[UploadedLine]:
    """WebVTT's cues as lines (the file-parsing steps of the WebVTT spec, minus the
    rendering ones: cue settings, regions and styles are read past)."""

    if not rows or _WEBVTT.fullmatch(rows[0]) is None:
        raise TranscriptFormatError("line 1: the file does not start with WEBVTT")
    index, count = 1, len(rows)

    def blank(at: int) -> bool:
        return not rows[at].strip()

    # The header runs to the first blank line.
    while index < count and not blank(index):
        index += 1
    lines: list[UploadedLine] = []
    while index < count:
        if blank(index):
            index += 1
            continue
        start = index
        if "-->" not in rows[index] and _SKIPPED_BLOCK.fullmatch(rows[index]):
            while index < count and not blank(index):
                index += 1
            continue
        if "-->" not in rows[index]:
            # A cue identifier: the timing is on the next line.
            index += 1
            if index >= count or "-->" not in rows[index]:
                raise TranscriptFormatError(
                    f"line {start + 1}: expected a cue timing (start --> end)"
                )
        number = index + 1
        # A timestamp, "-->" with optional whitespace around it, a timestamp, then
        # cue settings (read past).
        before, _, after = rows[index].partition("-->")
        end_and_settings = after.split()
        start_ms = _milliseconds(before.strip(), number)
        end_ms = _milliseconds(end_and_settings[0] if end_and_settings else "", number)
        if end_ms < start_ms:
            raise TranscriptFormatError(f"line {number}: the cue ends before it starts")
        index += 1
        payload: list[str] = []
        # A cue's text runs to a blank line, or to the next cue's timing.
        while index < count and not blank(index) and "-->" not in rows[index]:
            payload.append(rows[index])
            index += 1
        line = _cue_line(payload, start_ms, end_ms, number, default_speaker)
        if line is not None:
            _append(lines, line)
    return lines


# --- Evidence ------------------------------------------------------------------------------


def trace_id(call_id: str, recording_id: str) -> bytes:
    """The one trace a recording's uploaded transcript is written under."""

    value = f"{call_id}\x00{recording_id}".encode()
    return hashlib.sha256(_TRACE_NAMESPACE + value).digest()[:16]


def span_id(kind: str, ordinal: int) -> bytes:
    return hashlib.sha256(_SPAN_NAMESPACE + f"{kind}\x00{ordinal}".encode()).digest()[:8]


def header_span_id() -> bytes:
    """The ID of a transcript's ``earshot.upload.transcript`` span (in its trace)."""

    return span_id(TRANSCRIPT_SPAN, 0)


def _key_values(values: dict[str, str | int]):
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    return [
        KeyValue(
            key=key,
            value=(
                AnyValue(string_value=value)
                if isinstance(value, str)
                else AnyValue(int_value=value)
            ),
        )
        for key, value in sorted(values.items())
    ]


def transcript_spans(
    transcript: Transcript, *, call_id: str, recording_id: str, started_at: int
) -> list[OtlpSpan]:
    """The spans that record ``transcript`` of the recording ``recording_id`` (of the
    call ``call_id``, started at ``started_at`` Unix ns): the upload span, then one
    transcript-line span per line, in line order."""

    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    from .call_capture import OtlpSpan

    windows = []
    for position, line in enumerate(transcript.lines):
        if line.start_ms is None or line.end_ms is None:
            windows.append((started_at, started_at))
            continue
        start = started_at + line.start_ms * _NANOS_PER_MILLISECOND
        end = started_at + line.end_ms * _NANOS_PER_MILLISECOND
        if end > MAX_UNIX_NANO:
            raise TranscriptFormatError(
                f"line {position + 1} of the transcript ends later than the latest time "
                "Earshot stores (2^63 - 1 ns after 1970)"
            )
        windows.append((start, end))
    trace = trace_id(call_id, recording_id)
    parent = header_span_id()
    resource, scope = Resource(), InstrumentationScope(name=UPLOAD_SCOPE)

    def span(
        name: str, identifier: bytes, window: tuple[int, int], values: dict[str, str | int]
    ) -> OtlpSpan:
        message = Span(
            trace_id=trace,
            span_id=identifier,
            parent_span_id=b"" if identifier == parent else parent,
            name=name,
            kind=_SPAN_KIND_INTERNAL,
            start_time_unix_nano=window[0],
            end_time_unix_nano=window[1],
            attributes=_key_values(values),
        )
        return OtlpSpan(resource=resource, scope=scope, span=message)

    header = span(
        TRANSCRIPT_SPAN,
        parent,
        (min(start for start, _ in windows), max(end for _, end in windows)),
        {
            UPLOAD_FORMAT: transcript.format,
            UPLOAD_LINE_COUNT: len(transcript.lines),
            UPLOAD_SHA256: transcript.digest(),
        },
    )
    return [
        header,
        *(
            span(
                evidence.TRANSCRIPT_LINE,
                span_id(evidence.TRANSCRIPT_LINE, position),
                windows[position],
                {
                    evidence.TRANSCRIPT_SPEAKER: line.speaker,
                    evidence.TRANSCRIPT_TEXT: line.text,
                    evidence.TRANSCRIPT_TIMING: transcript.timing,
                    evidence.TRANSCRIPT_INDEX: position,
                },
            )
            for position, line in enumerate(transcript.lines)
        ),
    ]
