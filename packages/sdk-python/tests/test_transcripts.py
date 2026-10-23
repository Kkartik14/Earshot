"""Uploaded transcripts: WebVTT and plain text parsed into lines, and the lines as
evidence spans (FR-5).

The parser is pure; these tests feed it the bytes a client would send. The spans
are checked against the vocabulary the connectors write (``semconv/earshot.yaml``)
and the capture classes the store applies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from earshot.call_capture import attribute_capture_class, attribute_values
from earshot.connectors import evidence
from earshot.privacy import CaptureClass
from earshot.transcripts import (
    MAX_TRANSCRIPT_LINES,
    MAX_TRANSCRIPT_SOURCE_LINES,
    Transcript,
    TranscriptFormatError,
    parse_transcript,
    transcript_format,
    transcript_spans,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SECOND = 1_000_000_000
STARTED_AT = 1_800_000_000 * SECOND

VTT = """WEBVTT - an uploaded call

NOTE written by a transcription tool,
over two lines

STYLE
::cue { color: white }

greeting
00:01.000 --> 00:03.500 align:start position:10%
<v User>Hi, I'd like to book a table.</v>

00:00:04.250 --> 00:00:06.000
<v.loud Agent>Sure &amp; for <b>how many</b>
people?</v>

01:00:00.000 --> 01:00:01.000
agent: Anything else?
"""


def vtt(*cues: str) -> bytes:
    return ("WEBVTT\n\n" + "\n\n".join(cues) + "\n").encode()


def lines(transcript: Transcript) -> list[tuple[str, str, int | None, int | None]]:
    return [(line.speaker, line.text, line.start_ms, line.end_ms) for line in transcript.lines]


# --- formats -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/vtt", "vtt"),
        ("TEXT/VTT; charset=UTF-8", "vtt"),
        ("text/plain", "text"),
        ('text/plain;charset="utf-8"', "text"),
        ("text/plain; charset=latin-1", None),
        ("text/plain; charset=utf-16", None),
        ("application/json", None),
        ("", None),
    ],
)
def test_the_content_type_names_the_format_and_only_utf8_is_accepted(
    content_type: str, expected: str | None
) -> None:
    assert transcript_format(content_type) == expected


# --- WebVTT ------------------------------------------------------------------------------------


def test_a_webvtt_cue_is_a_measured_line_named_by_its_voice_or_prefix() -> None:
    transcript = parse_transcript(VTT.encode(), "vtt", default_speaker=None)

    assert transcript.format == "vtt"
    assert transcript.timing == "measured"
    assert lines(transcript) == [
        ("user", "Hi, I'd like to book a table.", 1_000, 3_500),
        ("agent", "Sure & for how many people?", 4_250, 6_000),
        ("agent", "Anything else?", 3_600_000, 3_601_000),
    ]


def test_webvtt_line_endings_and_a_byte_order_mark_do_not_change_the_lines() -> None:
    unix = parse_transcript(VTT.encode(), "vtt", default_speaker=None)
    windows = parse_transcript(
        b"\xef\xbb\xbf" + VTT.replace("\n", "\r\n").encode(), "vtt", default_speaker=None
    )

    assert windows == unix
    assert windows.digest() == unix.digest()


def test_a_cue_that_starts_right_after_the_last_without_a_blank_line_is_its_own_line() -> None:
    body = vtt("00:01.000 --> 00:02.000\n<v user>one\n00:02.000 --> 00:03.000\n<v agent>two")

    assert lines(parse_transcript(body, "vtt", default_speaker=None)) == [
        ("user", "one", 1_000, 2_000),
        ("agent", "two", 2_000, 3_000),
    ]


def test_a_cue_timing_reads_as_browsers_read_it_with_or_without_spaces() -> None:
    # The WebVTT parsing algorithm skips optional whitespace around the arrow.
    body = vtt(
        "00:01.000-->00:02.000\n<v user>one", "00:02.000\t-->  00:03.000 line:0\n<v agent>two"
    )

    assert lines(parse_transcript(body, "vtt", default_speaker=None)) == [
        ("user", "one", 1_000, 2_000),
        ("agent", "two", 2_000, 3_000),
    ]


def test_a_cue_with_no_text_is_no_line() -> None:
    body = vtt("00:01.000 --> 00:02.000\n<v user></v>", "00:02.000 --> 00:03.000\n<v agent>ok")

    assert lines(parse_transcript(body, "vtt", default_speaker=None)) == [
        ("agent", "ok", 2_000, 3_000)
    ]


def test_an_unvoiced_cue_is_the_speaker_of_a_one_speaker_recording() -> None:
    body = vtt("00:01.000 --> 00:02.000\nhello", "00:02.000 --> 00:03.000\n<v agent>hi")

    assert lines(parse_transcript(body, "vtt", default_speaker="user")) == [
        ("user", "hello", 1_000, 2_000),
        ("agent", "hi", 2_000, 3_000),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"00:01.000 --> 00:02.000\n<v user>hi\n", "does not start with WEBVTT"),
        (b"WEBVTTX\n\n00:01.000 --> 00:02.000\n<v user>hi\n", "does not start with WEBVTT"),
        (vtt("00:01.000 --> 00:02.000\nhello"), "line 3: name the speaker"),
        (vtt("00:01.000 --> 00:02.000\n<v Bob>hello"), "line 3: the speaker must be"),
        (vtt("00:01.000 -> 00:02.000\n<v user>hi"), "line 3: expected a cue timing"),
        (vtt("id\nsomething\n<v user>hi"), "line 3: expected a cue timing"),
        (vtt("00:01.00 --> 00:02.000\n<v user>hi"), "line 3: a cue time is"),
        (vtt("00:61.000 --> 01:02.000\n<v user>hi"), "line 3: a cue time is"),
        (vtt("00:03.000 --> 00:02.000\n<v user>hi"), "line 3: the cue ends before it starts"),
        (vtt("NOTE nothing but a note"), "no transcript lines"),
        (b"WEBVTT\n", "no transcript lines"),
    ],
)
def test_a_malformed_webvtt_file_is_refused_with_the_line_it_fails_at(
    body: bytes, message: str
) -> None:
    with pytest.raises(TranscriptFormatError, match=message):
        parse_transcript(body, "vtt", default_speaker=None)


def test_a_cue_past_the_latest_storable_time_is_refused() -> None:
    body = vtt("9999999999:00:00.000 --> 9999999999:00:01.000\n<v user>hi")

    # Parsed alone it is fine; as a span it must still be a 64-bit nanosecond time.
    transcript = parse_transcript(body, "vtt", default_speaker=None)
    with pytest.raises(TranscriptFormatError, match="later than"):
        transcript_spans(
            transcript, call_id="call_" + "0" * 32, recording_id="r", started_at=STARTED_AT
        )


# --- plain text --------------------------------------------------------------------------------


def test_each_plain_text_line_is_an_order_only_line() -> None:
    body = b"user: Hi there.\r\n\r\nAgent:Hello!  How can I help?\nUSER : time: 7pm\n"

    transcript = parse_transcript(body, "text", default_speaker=None)

    assert transcript.format == "text"
    assert transcript.timing == "order_only"
    assert lines(transcript) == [
        ("user", "Hi there.", None, None),
        ("agent", "Hello! How can I help?", None, None),
        ("user", "time: 7pm", None, None),
    ]


def test_plain_text_collapses_unicode_whitespace_to_one_space() -> None:
    body = "user: one\u2003two\u2009three".encode()

    assert lines(parse_transcript(body, "text", default_speaker=None)) == [
        ("user", "one two three", None, None)
    ]


def test_an_unlabelled_plain_text_line_is_the_speaker_of_a_one_speaker_recording() -> None:
    body = "Bonjour: je voudrais réserver.\nagent: Oui.".encode()

    assert lines(parse_transcript(body, "text", default_speaker="user")) == [
        ("user", "Bonjour: je voudrais réserver.", None, None),
        ("agent", "Oui.", None, None),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"user: hi\n\nhello\n", "line 3: start the line with"),
        (b"user:\n", "line 1: the line has no text"),
        (b"\n  \n", "no transcript lines"),
        (b"user: caf\xe9\n", "not UTF-8"),
        (b"user: a\x00b\n", "line 1: control characters"),
    ],
)
def test_a_malformed_plain_text_file_is_refused_with_the_line_it_fails_at(
    body: bytes, message: str
) -> None:
    with pytest.raises(TranscriptFormatError, match=message):
        parse_transcript(body, "text", default_speaker=None)


def test_a_transcript_of_more_lines_than_the_cap_is_refused() -> None:
    body = b"user: hi\n" * (MAX_TRANSCRIPT_LINES + 1)

    with pytest.raises(TranscriptFormatError, match=f"more than {MAX_TRANSCRIPT_LINES}"):
        parse_transcript(body, "text", default_speaker=None)
    assert len(parse_transcript(body[9:], "text", default_speaker=None).lines) == (
        MAX_TRANSCRIPT_LINES
    )


def test_a_transcript_with_too_many_raw_lines_is_refused_before_row_materialization() -> None:
    body = b"\n" * MAX_TRANSCRIPT_SOURCE_LINES

    with pytest.raises(
        TranscriptFormatError,
        match=f"more than {MAX_TRANSCRIPT_SOURCE_LINES} source lines",
    ):
        parse_transcript(body, "text", default_speaker=None)


# --- identity ------------------------------------------------------------------------------------


def test_the_digest_is_the_lines_and_their_format() -> None:
    first = parse_transcript(b"user: hi\nagent: hello\n", "text", default_speaker=None)
    spaced = parse_transcript(b"user:   hi\r\n\r\nagent:hello", "text", default_speaker=None)
    changed = parse_transcript(b"user: hi\nagent: hello!\n", "text", default_speaker=None)
    as_vtt = Transcript("vtt", first.lines)

    assert spaced.digest() == first.digest()
    assert changed.digest() != first.digest()
    assert as_vtt.digest() != first.digest()
    assert len(first.digest()) == 64


# --- spans ---------------------------------------------------------------------------------------


def _attributes(span) -> dict[str, object]:
    return attribute_values(span.span.attributes)


def test_the_lines_become_transcript_line_spans_under_one_upload_span() -> None:
    transcript = parse_transcript(VTT.encode(), "vtt", default_speaker=None)

    header, *line_spans = transcript_spans(
        transcript, call_id="call_" + "a" * 32, recording_id="rec-1", started_at=STARTED_AT
    )

    assert {span.scope.name for span in (header, *line_spans)} == {"earshot.upload"}
    assert header.span.name == "earshot.upload.transcript"
    assert header.span.parent_span_id == b""
    assert _attributes(header) == {
        "earshot.upload.format": "vtt",
        "earshot.upload.line_count": 3,
        "earshot.upload.sha256": transcript.digest(),
    }
    assert (header.span.start_time_unix_nano, header.span.end_time_unix_nano) == (
        STARTED_AT + 1_000 * 1_000_000,
        STARTED_AT + 3_601_000 * 1_000_000,
    )
    assert [span.span.name for span in line_spans] == [evidence.TRANSCRIPT_LINE] * 3
    assert {span.span.trace_id for span in line_spans} == {header.span.trace_id}
    assert {span.span.parent_span_id for span in line_spans} == {header.span.span_id}
    second = line_spans[1]
    assert _attributes(second) == {
        evidence.TRANSCRIPT_SPEAKER: "agent",
        evidence.TRANSCRIPT_TEXT: "Sure & for how many people?",
        evidence.TRANSCRIPT_TIMING: "measured",
        evidence.TRANSCRIPT_INDEX: 1,
    }
    assert (second.span.start_time_unix_nano, second.span.end_time_unix_nano) == (
        STARTED_AT + 4_250 * 1_000_000,
        STARTED_AT + 6_000 * 1_000_000,
    )


def test_order_only_lines_sit_at_the_recording_start_and_keep_their_index() -> None:
    transcript = parse_transcript(b"user: a\nagent: b\n", "text", default_speaker=None)

    header, *line_spans = transcript_spans(
        transcript, call_id="call_" + "a" * 32, recording_id="rec-1", started_at=STARTED_AT
    )

    for index, span in enumerate([header, *line_spans]):
        assert (span.span.start_time_unix_nano, span.span.end_time_unix_nano) == (
            STARTED_AT,
            STARTED_AT,
        )
        if span is not header:
            assert _attributes(span)[evidence.TRANSCRIPT_INDEX] == index - 1
            assert _attributes(span)[evidence.TRANSCRIPT_TIMING] == "order_only"


def test_the_same_transcript_of_the_same_recording_is_the_same_spans() -> None:
    transcript = parse_transcript(VTT.encode(), "vtt", default_speaker=None)
    call_id = "call_" + "a" * 32

    def payloads(recording_id: str) -> list[bytes]:
        return [
            span.span.SerializeToString(deterministic=True)
            for span in transcript_spans(
                transcript, call_id=call_id, recording_id=recording_id, started_at=STARTED_AT
            )
        ]

    assert payloads("rec-1") == payloads("rec-1")
    first, other = (
        transcript_spans(transcript, call_id=call_id, recording_id=name, started_at=STARTED_AT)[0]
        for name in ("rec-1", "rec-2")
    )
    # One trace per recording: a call's two recordings each keep their own transcript.
    assert first.span.trace_id != other.span.trace_id


def test_line_text_is_transcript_content_and_the_upload_span_is_metadata() -> None:
    transcript = parse_transcript(b"user: hi\n", "text", default_speaker=None)
    header, line = transcript_spans(
        transcript, call_id="call_" + "a" * 32, recording_id="rec-1", started_at=STARTED_AT
    )

    assert {attribute_capture_class(key) for key in _attributes(header)} == {CaptureClass.METADATA}
    # metadata_only would keep none of a line: the API refuses it rather than store nothing.
    assert {attribute_capture_class(key) for key in _attributes(line)} == {CaptureClass.TRANSCRIPT}


def test_every_upload_span_and_attribute_is_registered_in_semconv() -> None:
    registry = yaml.safe_load((ROOT / "semconv" / "earshot.yaml").read_text())
    groups = {group["id"]: group for group in registry["groups"]}
    registered = {item["id"] for item in groups["registry.earshot"]["attributes"]}
    transcript = parse_transcript(b"user: hi\n", "text", default_speaker=None)
    header, line = transcript_spans(
        transcript, call_id="call_" + "a" * 32, recording_id="rec-1", started_at=STARTED_AT
    )

    assert set(_attributes(header)) <= registered
    assert set(_attributes(line)) <= registered
    upload = groups["span.earshot.upload.transcript"]
    assert {item["ref"] for item in upload["attributes"]} == set(_attributes(header))
