"""The pure half of the recordings API (FR-9): IDs, media types, WAV headers, ranges.

WAV bodies are real files written by the standard ``wave`` module (and, for the
extensible format, by hand from the RIFF layout); every header is fed to the
inspector in pieces, as an upload streams in.
"""

from __future__ import annotations

import io
import struct
import time
import wave

import pytest

from earshot.recordings import (
    MAX_RANGE_HEADER_LENGTH,
    RECORDING_CONTENT_TYPES,
    ByteRange,
    RangeNotSatisfiable,
    WavHeaderError,
    WavInspector,
    parse_byte_range,
    valid_recording_id,
)

pytestmark = pytest.mark.unit


def wav_bytes(*, channels: int = 1, rate: int = 16_000, width: int = 2, frames: int = 16_000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(b"\x01\x02" * (frames * channels * width // 2))
    return buffer.getvalue()


def extensible_wav(*, channels: int, frames: int = 800) -> bytes:
    """A WAVE_FORMAT_EXTENSIBLE file with a LIST chunk before ``fmt``."""

    rate, width = 8_000, 2
    block = channels * width
    fmt = struct.pack(
        "<HHIIHHHHIH14s",
        0xFFFE,
        channels,
        rate,
        rate * block,
        block,
        width * 8,
        22,
        width * 8,
        (1 << channels) - 1,
        1,  # PCM sub-format GUID, first two bytes
        b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71",
    )
    data = b"\x00" * (frames * block)
    chunks = (
        b"LIST" + struct.pack("<I", 4) + b"INFO"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )  # fmt: skip
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def inspect(body: bytes, piece: int = 7) -> WavInspector:
    inspector = WavInspector()
    for start in range(0, len(body), piece):
        inspector.feed(body[start : start + piece])
    inspector.finish(len(body))
    return inspector


# --- recording IDs and media types ------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["a", "call-1.user", "0", "A.b_c-d", "x" * 128, "7f1c0d6e-3a5b-4c1e-9d2f-0b6a8e4c2d10"],
)
def test_recording_ids_follow_the_bundle_id_shape(value) -> None:
    assert valid_recording_id(value)


@pytest.mark.parametrize(
    "value", ["", ".", "..", ".hidden", "-x", "_x", "a/b", "a b", "x" * 129, "é", "a\x00"]
)
def test_recording_ids_outside_the_pattern_are_refused(value) -> None:
    assert not valid_recording_id(value)


def test_the_five_month_one_media_types_are_the_only_content_types() -> None:
    assert RECORDING_CONTENT_TYPES == (
        "audio/wav",
        "audio/ogg",
        "audio/mpeg",
        "audio/webm",
        "audio/flac",
    )


# --- WAV headers --------------------------------------------------------------------------


@pytest.mark.parametrize("piece", [1, 7, 44, 4096])
def test_a_mono_wav_reports_its_format_and_duration_whatever_the_chunking(piece) -> None:
    body = wav_bytes(rate=16_000, frames=24_000)

    inspector = inspect(body, piece)

    assert (inspector.channels, inspector.sample_rate) == (1, 16_000)
    assert inspector.duration_ms() == 1500


@pytest.mark.parametrize("channels", [2, 6])
def test_a_multi_channel_wav_is_known_not_mono_from_its_header_alone(channels) -> None:
    body = wav_bytes(channels=channels)
    inspector = WavInspector()

    inspector.feed(body[:44])

    assert inspector.header_complete
    assert inspector.channels == channels


def test_an_extensible_wav_after_a_list_chunk_is_read_too() -> None:
    assert inspect(extensible_wav(channels=1, frames=800)).duration_ms() == 100
    assert inspect(extensible_wav(channels=2)).channels == 2


def test_a_streamed_wav_with_an_open_ended_data_size_takes_its_duration_from_the_body() -> None:
    body = bytearray(wav_bytes(rate=8_000, frames=4_000))
    struct.pack_into("<I", body, 40, 0xFFFFFFFF)  # the data chunk size of a live recorder

    assert inspect(bytes(body)).duration_ms() == 500


def coded_wav(fmt: bytes, data_bytes: int, *, fact_samples: int | None = None) -> bytes:
    """A mono WAV with this ``fmt`` chunk (and a ``fact`` chunk, as coded WAVs carry)."""

    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if fact_samples is not None:
        chunks += b"fact" + struct.pack("<II", 4, fact_samples)
    chunks += b"data" + struct.pack("<I", data_bytes) + b"\x00" * data_bytes
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


# (format tag, sample rate, byte rate, block align, bits, extra fmt bytes)
GSM_610 = struct.pack("<HHIIHHHH", 0x0031, 1, 8_000, 1_625, 65, 0, 2, 320)  # Asterisk wav49
IMA_ADPCM = struct.pack("<HHIIHHHH", 0x0011, 1, 8_000, 4_055, 256, 4, 2, 505)
MU_LAW = struct.pack("<HHIIHH", 0x0007, 1, 8_000, 8_000, 1, 8)
MPEG_LAYER_3 = struct.pack("<HHIIHHH", 0x0055, 1, 8_000, 1_000, 1, 0, 0)  # may be VBR
GSM_NO_BYTE_RATE = struct.pack("<HHIIHHHH", 0x0031, 1, 8_000, 0, 65, 0, 2, 320)


@pytest.mark.parametrize(
    ("fmt", "data_bytes", "expected"),
    [
        (GSM_610, 1_625, 1_000),
        (IMA_ADPCM, 4_055, 1_000),
        (MU_LAW, 8_000, 1_000),
        (MPEG_LAYER_3, 1_000, None),
        (GSM_NO_BYTE_RATE, 1_625, None),
    ],
    ids=["ms-gsm", "ima-adpcm", "mu-law", "mp3-unreliable", "no-byte-rate"],
)
def test_a_coded_wav_has_its_true_duration_or_none(fmt, data_bytes, expected) -> None:
    """Tribunal round 1, B-5: block-coded telephony WAVs use the byte rate; a format
    whose byte rate is not reliable has no duration rather than a wrong one."""

    assert inspect(coded_wav(fmt, data_bytes, fact_samples=8_000)).duration_ms() == expected


def test_a_duration_past_the_int64_nanosecond_range_is_none() -> None:
    body = bytearray(wav_bytes(rate=1, width=1, frames=4))
    struct.pack_into("<I", body, 40, 0xFFFFFFFF)  # open-ended: the duration is the body's
    inspector = WavInspector()
    inspector.feed(bytes(body))

    inspector.finish(10**13)  # 10^16 ms at one byte a second: past 2^63 - 1 ns

    assert inspector.duration_ms() is None


def test_trailing_chunks_after_the_data_chunk_do_not_count_as_audio() -> None:
    body = wav_bytes(rate=8_000, frames=8_000) + b"LIST" + struct.pack("<I", 1000) + b"\0" * 1000

    assert inspect(body).duration_ms() == 1000


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"RIFF",
        b"OggS" + b"\0" * 60,
        b"RIFF\x24\x00\x00\x00WAVEdata\x00\x00\x00\x00",  # data before fmt
        b"RIFF\x24\x00\x00\x00WAVEfmt \x02\x00\x00\x00\x01\x00",  # fmt too short
    ],
    ids=["empty", "truncated", "not-riff", "data-before-fmt", "short-fmt"],
)
def test_a_body_that_is_not_a_readable_wav_is_refused(body) -> None:
    with pytest.raises(WavHeaderError):
        inspect(body)


@pytest.mark.parametrize("junk", [1 << 30, 2 * 1024 * 1024 - 64])
def test_a_header_that_cannot_end_within_its_bound_is_refused(junk) -> None:
    inspector = WavInspector()

    with pytest.raises(WavHeaderError):
        inspector.feed(b"RIFF\xff\xff\xff\xffWAVE" + b"JUNK" + struct.pack("<I", junk))
        for _ in range(3):
            inspector.feed(b"\0" * (1024 * 1024))


# --- byte ranges ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-0", ByteRange(0, 1)),
        ("bytes=10-19", ByteRange(10, 20)),
        ("bytes=10-", ByteRange(10, 100)),
        ("bytes=90-500", ByteRange(90, 100)),
        ("bytes=-5", ByteRange(95, 100)),
        ("bytes=-500", ByteRange(0, 100)),
        ("BYTES = 1-2", ByteRange(1, 3)),
        ("bytes=0-1,5-6", None),  # several ranges: answered whole
        ("items=0-5", None),  # another unit: ignored
        ("bytes=5-1", None),  # invalid: ignored, as RFC 9110 allows
        ("bytes=abc", None),
        ("bytes=0-1\n", ByteRange(0, 2)),  # surrounding whitespace, as before
        ("bytes=٣-5", None),  # only ASCII digits are positions
        # Positions longer than any object size (tribunal round 1, B-2): the end and
        # a suffix are clamped to the object, as RFC 9110 reads them; leading zeros
        # are only zeros.
        ("bytes=0-" + "9" * 200, ByteRange(0, 100)),
        ("bytes=-" + "9" * 200, ByteRange(0, 100)),
        ("bytes=" + "0" * 100 + "5-" + "0" * 100 + "9", ByteRange(5, 10)),
    ],
)
def test_one_byte_range_is_honoured_and_anything_else_is_answered_whole(header, expected) -> None:
    assert parse_byte_range(header, 100) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=100-",
        "bytes=150-200",
        "bytes=-0",
        "bytes=" + "9" * 200 + "-",
        "bytes=" + "9" * 100 + "-" + "9" * 101,
        "bytes=" + "9" * 20 + "-",
    ],
)
def test_a_range_past_the_end_is_not_satisfiable(header) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range(header, 100)


@pytest.mark.parametrize(
    "header",
    [
        "bytes=x" + " " * 100_000 + "x",  # tribunal round 2, N-2: quadratic in a regex
        "bytes=0-" + "9" * 5000,
        " " * MAX_RANGE_HEADER_LENGTH + "bytes=0-1",
    ],
    ids=["spaces", "digits", "padded"],
)
def test_a_range_header_longer_than_its_bound_is_refused_in_linear_time(header) -> None:
    started = time.perf_counter()
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range(header, 100)
    assert time.perf_counter() - started < 0.05


def test_a_range_header_at_its_bound_is_read() -> None:
    header = "bytes=0-1".ljust(MAX_RANGE_HEADER_LENGTH)

    assert parse_byte_range(header, 100) == ByteRange(0, 2)


def test_any_range_of_an_empty_object_is_not_satisfiable() -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range("bytes=0-", 0)
