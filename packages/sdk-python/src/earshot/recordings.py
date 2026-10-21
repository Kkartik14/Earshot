"""The protocol half of the recordings API (FR-9): IDs, media types, WAV headers, ranges.

Pure functions and small state machines with no storage and no HTTP server, so
the API and its tests share one reading of each rule:

- ``valid_recording_id``: a client recording ID has the ``BundleId`` shape
  ``^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`` (no ``.`` or ``..``, no path separator).
  It names the recording inside its project; it never becomes part of a blob key.
- ``valid_start_unix_nano``: a recording's start, a decimal Unix nanosecond time in
  [1, 2^63 - 1] (the catalog's signed 64-bit range).
- ``RECORDING_CONTENT_TYPES``: the five media types Month 1 accepts.
- ``WavInspector``: reads a WAV header from a body as it streams in, so an upload
  that is not mono is refused from its header (Month 1 stores mono tracks only),
  and gives the duration once the body is complete (or none, never a wrong one).
- ``parse_byte_range``: the one ``Range: bytes=…`` reading both blob backends use.

This module imports only the standard library.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

RECORDING_CONTENT_TYPES = ("audio/wav", "audio/ogg", "audio/mpeg", "audio/webm", "audio/flac")
WAV_CONTENT_TYPE = "audio/wav"
RECORDING_SPEAKERS = ("user", "agent", "mixed")
RECORDING_STATES = ("pending", "stored", "failed")
LABEL_MAX_LENGTH = 128
_RECORDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# A WAV header (every chunk before ``data``) must end within this many bytes.
WAV_HEADER_LIMIT = 1024 * 1024
_RIFF_PREAMBLE = 12
_CHUNK_HEADER = 8
_FMT_MINIMUM = 16
_OPEN_ENDED_SIZES = (0, 0xFFFFFFFF)
# WAVE format tags. Sample formats (PCM, IEEE float, A-law, µ-law) last
# ``bytes / (sample_rate * block_align)``. Constant-rate block codecs (MS ADPCM, IMA
# ADPCM, GSM 6.10) last ``bytes / byte_rate``. Any other codec may be variable-rate,
# so its duration is unknown (``None``) rather than a guess.
_SAMPLE_FORMATS = frozenset({0x0001, 0x0003, 0x0006, 0x0007})
_CONSTANT_RATE_CODECS = frozenset({0x0002, 0x0011, 0x0031})
_EXTENSIBLE = 0xFFFE
_EXTENSIBLE_FMT_MINIMUM = 26  # through the first two bytes of the SubFormat GUID
# The catalog keeps Unix nanosecond times in signed 64-bit columns, so a recording's
# start is in [1, 2^63 - 1], written in decimal ASCII without sign or leading zero
# (as the OTLP receiver bounds span times).
MAX_UNIX_NANO = 2**63 - 1
_START_UNIX_NANO = re.compile(r"[1-9][0-9]{0,18}")
# A duration in milliseconds whose nanoseconds still fit int64, so a Recording's end
# (its start, at most 2^63 - 1, plus this) stays inside the contract's uint64 times.
MAX_DURATION_MS = MAX_UNIX_NANO // 1_000_000


def valid_recording_id(value: object) -> bool:
    """Whether ``value`` is a recording ID a client may choose."""

    return isinstance(value, str) and _RECORDING_ID.fullmatch(value) is not None


def valid_start_unix_nano(value: object) -> bool:
    """Whether ``value`` is a recording's start: a decimal Unix nanosecond time in
    [1, 2^63 - 1]. The one rule the API and the store both apply."""

    return (
        isinstance(value, str)
        and _START_UNIX_NANO.fullmatch(value) is not None
        and int(value) <= MAX_UNIX_NANO
    )


def upload_path(recording_id: str) -> str:
    """The relative path a recording's bytes are uploaded to and read from."""

    return f"/v1/recordings/{recording_id}/content"


class WavHeaderError(ValueError):
    """The body does not start with a WAV header this server can read."""


class WavInspector:
    """Read the ``fmt`` chunk and find the ``data`` chunk of a streamed WAV body.

    ``feed`` the body's chunks in order; once ``header_complete`` is true the
    format is known (``channels``, ``sample_rate``) and later chunks are ignored.
    ``finish(total_size)`` checks the header was complete and fixes the duration
    (``None`` when the format does not give it reliably).
    Chunks before ``data`` must end within ``WAV_HEADER_LIMIT`` bytes; any other
    shape (not RIFF/WAVE, ``data`` before ``fmt``, a short ``fmt``) raises
    ``WavHeaderError``.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.header_complete = False
        self.channels = 0
        self.sample_rate = 0
        self.block_align = 0
        self.data_offset = 0
        self.data_size = 0
        self.format_tag = 0
        self.byte_rate = 0
        self._fmt_seen = False
        self._finished = False
        self._duration_ms: int | None = None

    def feed(self, chunk: bytes) -> None:
        if self.header_complete or not chunk:
            return
        self._buffer.extend(chunk[: WAV_HEADER_LIMIT + 1 - len(self._buffer)])
        self._parse()
        if not self.header_complete and len(self._buffer) > WAV_HEADER_LIMIT:
            raise WavHeaderError("the WAV header does not end within its bound")

    def finish(self, total_size: int) -> None:
        if not self.header_complete:
            raise WavHeaderError("the body ends before its WAV header does")
        available = max(0, total_size - self.data_offset)
        audio = (
            available
            if self.data_size in _OPEN_ENDED_SIZES or self.data_size > available
            else self.data_size
        )
        if self.format_tag in _SAMPLE_FORMATS:
            bytes_per_second = self.sample_rate * self.block_align
        elif self.format_tag in _CONSTANT_RATE_CODECS and self.byte_rate > 0:
            bytes_per_second = self.byte_rate
        else:
            bytes_per_second = 0
        duration = audio * 1000 // bytes_per_second if bytes_per_second else None
        if duration is not None and duration > MAX_DURATION_MS:
            duration = None
        self._duration_ms = duration
        self._finished = True

    def duration_ms(self) -> int | None:
        """The audio's duration, or ``None`` when the format does not give it reliably
        (a codec that may be variable-rate, no byte rate, or past ``MAX_DURATION_MS``)."""

        if not self._finished:
            raise ValueError("finish() the inspector before reading the duration")
        return self._duration_ms

    def _parse(self) -> None:
        buffer = self._buffer
        if len(buffer) < _RIFF_PREAMBLE:
            return
        if buffer[0:4] != b"RIFF" or buffer[8:12] != b"WAVE":
            raise WavHeaderError("the body is not a RIFF/WAVE file")
        offset = _RIFF_PREAMBLE
        while len(buffer) >= offset + _CHUNK_HEADER:
            name = bytes(buffer[offset : offset + 4])
            (size,) = struct.unpack_from("<I", buffer, offset + 4)
            body = offset + _CHUNK_HEADER
            if name == b"data":
                if not self._fmt_seen:
                    raise WavHeaderError("the WAV data chunk comes before its fmt chunk")
                self.data_offset, self.data_size = body, size
                self.header_complete = True
                return
            end = body + size + (size & 1)
            if end > WAV_HEADER_LIMIT:
                raise WavHeaderError("the WAV header does not end within its bound")
            if len(buffer) < body + size:
                return
            if name == b"fmt ":
                self._read_fmt(bytes(buffer[body : body + size]))
            offset = end

    def _read_fmt(self, fmt: bytes) -> None:
        if len(fmt) < _FMT_MINIMUM:
            raise WavHeaderError("the WAV fmt chunk is too short")
        tag, channels, sample_rate, byte_rate, block_align = struct.unpack_from("<HHIIH", fmt)
        if channels < 1 or sample_rate < 1 or block_align < 1:
            raise WavHeaderError("the WAV fmt chunk describes no audio")
        if tag == _EXTENSIBLE and len(fmt) >= _EXTENSIBLE_FMT_MINIMUM:
            (tag,) = struct.unpack_from("<H", fmt, 24)  # the SubFormat GUID's format tag
        self.channels, self.sample_rate, self.block_align = channels, sample_rate, block_align
        self.format_tag, self.byte_rate = tag, byte_rate
        self._fmt_seen = True


@dataclass(frozen=True, slots=True)
class ByteRange:
    """The bytes ``[start, end)`` of an object."""

    start: int
    end: int

    def content_range(self, size: int) -> str:
        return f"bytes {self.start}-{self.end - 1}/{size}"


class RangeNotSatisfiable(ValueError):
    """The one requested range lies past the end of the object (416)."""


class RangeTooLong(RangeNotSatisfiable):
    """The ``Range`` header is longer than ``MAX_RANGE_HEADER_LENGTH`` (416): no
    single range needs that much, so it is refused before it is read."""


# A single range with two 19-digit positions is under 50 characters; anything over
# this bound is refused unread (RFC 9110 §15.5.17 allows 416 for an excessive
# request), so reading a header never costs more than this many characters.
MAX_RANGE_HEADER_LENGTH = 256
_ASCII_DIGITS = frozenset("0123456789")
# An object is smaller than 2^63 bytes (19 digits), so a longer position is past any
# end. It reads as 2^63 instead of being converted.
_MAX_POSITION_DIGITS = 19
_PAST_ANY_END = 1 << 63


def _position(digits: str) -> int:
    significant = digits.lstrip("0") or "0"
    return int(significant) if len(significant) <= _MAX_POSITION_DIGITS else _PAST_ANY_END


def _digits(value: str) -> bool:
    """Whether ``value`` is empty or ASCII digits only (``"٣"`` is not a position)."""

    return all(character in _ASCII_DIGITS for character in value)


def parse_byte_range(header: str | None, size: int) -> ByteRange | None:
    """The single byte range a ``Range`` header asks of a ``size``-byte object.

    ``None`` means "answer with the whole object": no header, another unit,
    several ranges, or a malformed or invalid range (RFC 9110 §14.2 lets a
    server ignore those). A valid range that starts at or past the end raises
    ``RangeNotSatisfiable``; one that runs past the end is shortened. A header
    longer than ``MAX_RANGE_HEADER_LENGTH`` raises ``RangeTooLong``. The header
    is read with string operations, never a backtracking pattern, so the cost is
    linear in its (bounded) length.
    """

    if header is None:
        return None
    if len(header) > MAX_RANGE_HEADER_LENGTH:
        raise RangeTooLong("the Range header is longer than its bound")
    unit, equals, spec = header.partition("=")
    unit, spec = unit.strip(), spec.strip()
    if not equals or not unit.isascii() or unit.lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.partition("-")
    if not dash or first == last == "" or not (_digits(first) and _digits(last)):
        return None
    if first == "":
        suffix = _position(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable("an empty suffix range")
        return ByteRange(max(0, size - suffix), size)
    start = _position(first)
    if last != "" and _position(last) < start:
        return None
    if start >= size:
        raise RangeNotSatisfiable("the range starts past the end")
    end = size if last == "" else min(_position(last) + 1, size)
    return ByteRange(start, end)
