"""The protocol half of the recordings API (FR-9, FR-1): IDs, media types, headers, ranges.

Pure functions and small state machines with no storage and no HTTP server, so
the API and its tests share one reading of each rule:

- ``valid_recording_id``: a client recording ID has the ``BundleId`` shape
  ``^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`` (no ``.`` or ``..``, no path separator).
  It names the recording inside its project; it never becomes part of a blob key.
- ``valid_start_unix_nano``: a recording's start, a decimal Unix nanosecond time in
  [1, 2^63 - 1] (the catalog's signed 64-bit range).
- ``RECORDING_CONTENT_TYPES``: the five media types a recording may have.
- ``valid_channel_speakers``: a ``mixed`` recording's channel map names each
  channel's speaker in order, ``user`` and ``agent`` at most once each.
- ``media_inspector``: the header reader of a WAV, FLAC or Ogg Opus body
  (``WavInspector``, ``FlacInspector``, ``OggInspector``). Each reads its header as
  the body streams in, so an upload with the wrong channel count is refused from
  its header, and gives the duration once the body is complete (or none, never a
  wrong one). MP3 and WebM bodies are not read.
- ``parse_byte_range``: the one ``Range: bytes=…`` reading both blob backends use.

This module imports only the standard library.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

RECORDING_CONTENT_TYPES = ("audio/wav", "audio/ogg", "audio/mpeg", "audio/webm", "audio/flac")
WAV_CONTENT_TYPE = "audio/wav"
FLAC_CONTENT_TYPE = "audio/flac"
OGG_CONTENT_TYPE = "audio/ogg"
RECORDING_SPEAKERS = ("user", "agent", "mixed")
# The speaker a channel of a ``mixed`` recording may have, each at most once.
CHANNEL_SPEAKERS = ("user", "agent")
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


def valid_channel_speakers(value: object) -> bool:
    """Whether ``value`` is a channel map: one or two of ``user`` and ``agent``, in
    channel order, neither twice. Its length must also be the file's channel count,
    which only the upload's header shows."""

    return (
        isinstance(value, list | tuple)
        and 1 <= len(value) <= len(CHANNEL_SPEAKERS)
        and all(isinstance(item, str) and item in CHANNEL_SPEAKERS for item in value)
        and len(set(value)) == len(value)
    )


def upload_path(recording_id: str) -> str:
    """The relative path a recording's bytes are uploaded to and read from."""

    return f"/v1/recordings/{recording_id}/content"


class RecordingHeaderError(ValueError):
    """The body does not start with a header of its media type this server can read."""


class WavHeaderError(RecordingHeaderError):
    """The body does not start with a WAV header this server can read."""


class FlacHeaderError(RecordingHeaderError):
    """The body does not start with a FLAC header this server can read."""


class OggHeaderError(RecordingHeaderError):
    """The body does not start with an Ogg Opus header this server can read."""


def _bounded_duration_ms(milliseconds: int) -> int | None:
    return milliseconds if milliseconds <= MAX_DURATION_MS else None


class WavInspector:
    """Read the ``fmt`` chunk and find the ``data`` chunk of a streamed WAV body.

    ``format_name`` names the format in the API's error codes.

    ``feed`` the body's chunks in order; once ``header_complete`` is true the
    format is known (``channels``, ``sample_rate``) and later chunks are ignored.
    ``finish(total_size)`` checks the header was complete and fixes the duration
    (``None`` when the format does not give it reliably).
    Chunks before ``data`` must end within ``WAV_HEADER_LIMIT`` bytes; any other
    shape (not RIFF/WAVE, ``data`` before ``fmt``, a short ``fmt``) raises
    ``WavHeaderError``.
    """

    format_name = "wav"

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


class FlacInspector:
    """Read the STREAMINFO block at the start of a streamed FLAC body (RFC 9639 §8.2).

    A FLAC stream is ``fLaC`` and then its metadata blocks, STREAMINFO first, so the
    first 42 bytes give the channel count, the sample rate and the total sample
    count. The duration is the total as STREAMINFO declares it, or ``None`` when it
    declares none (0, "unknown"). Any other start raises ``FlacHeaderError``.
    """

    format_name = "flac"
    _HEADER = 42  # "fLaC", a 4-byte block header and the 34-byte STREAMINFO

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.header_complete = False
        self.channels = 0
        self.sample_rate = 0
        self.total_samples = 0
        self._finished = False

    def feed(self, chunk: bytes) -> None:
        if self.header_complete or not chunk:
            return
        self._buffer.extend(chunk[: self._HEADER - len(self._buffer)])
        if len(self._buffer) >= 4 and self._buffer[:4] != b"fLaC":
            raise FlacHeaderError("the body is not a FLAC stream")
        if len(self._buffer) < self._HEADER:
            return
        block_type = self._buffer[4] & 0x7F
        block_size = int.from_bytes(self._buffer[5:8], "big")
        if block_type != 0 or block_size != 34:
            raise FlacHeaderError("the FLAC stream does not start with its STREAMINFO block")
        (packed,) = struct.unpack_from(">Q", self._buffer, 18)
        self.sample_rate = packed >> 44
        self.channels = ((packed >> 41) & 0x7) + 1
        self.total_samples = packed & 0xF_FFFF_FFFF
        if self.sample_rate == 0:
            raise FlacHeaderError("the FLAC STREAMINFO describes no audio")
        self.header_complete = True

    def finish(self, total_size: int) -> None:
        if not self.header_complete:
            raise FlacHeaderError("the body ends before its FLAC header does")
        self._finished = True

    def duration_ms(self) -> int | None:
        if not self._finished:
            raise ValueError("finish() the inspector before reading the duration")
        if self.total_samples == 0:
            return None
        return _bounded_duration_ms(self.total_samples * 1000 // self.sample_rate)


class OggInspector:
    """Walk the pages of a streamed Ogg Opus body (RFC 3533, RFC 7845).

    The first page must begin a logical stream (BOS) whose first packet is an
    ``OpusHead``: it gives the channel count and the pre-skip. Every later page's
    header is read and its body skipped, so the walk costs a few bytes a page. The
    duration is the last granule position less the pre-skip, at 48 kHz (RFC 7845
    §4), or ``None`` when the body does not end on a page boundary, carries a
    second logical stream, or loses the page sync. Ogg holding another codec (Vorbis,
    say) raises ``OggHeaderError``: its channel count is not read.
    """

    format_name = "ogg"
    _PAGE_HEADER = 27
    _NO_GRANULE = 0xFFFF_FFFF_FFFF_FFFF
    _OPUS_RATE = 48_000
    _OPUS_HEAD = 19  # "OpusHead", version, channels, pre-skip, rate, gain, mapping

    def __init__(self) -> None:
        self.header_complete = False
        self.channels = 0
        self.pre_skip = 0
        self._header = bytearray()  # the page header being read
        self._skip = 0  # body bytes of the current page still to pass
        self._first_body: bytearray | None = None  # the first page's body, read whole
        self._serial: bytes | None = None
        self._granule: int | None = None
        self._other_stream = False
        self._lost = False
        self._finished = False
        self._duration_ms: int | None = None

    def feed(self, chunk: bytes) -> None:
        if self._lost or not chunk:
            return
        view, position = memoryview(chunk), 0
        while position < len(view):
            if self._skip:
                taken = min(self._skip, len(view) - position)
                if self._first_body is not None:
                    self._first_body += view[position : position + taken]
                self._skip -= taken
                position += taken
                if not self._skip and self._first_body is not None:
                    self._read_head(bytes(self._first_body))
                    self._first_body = None
                continue
            wanted = self._PAGE_HEADER
            if len(self._header) >= self._PAGE_HEADER:
                wanted += self._header[26]
            taken = min(wanted - len(self._header), len(view) - position)
            self._header += view[position : position + taken]
            position += taken
            if len(self._header) >= 4 and self._header[:4] != b"OggS":
                if not self.header_complete:
                    raise OggHeaderError("the body is not an Ogg stream")
                self._lost = True  # a damaged page: the duration is unknown
                return
            complete = len(self._header) >= self._PAGE_HEADER and len(self._header) == (
                self._PAGE_HEADER + self._header[26]
            )
            if complete:
                self._page(bytes(self._header))
                self._header.clear()
                if not self._skip and self._first_body is not None:
                    raise OggHeaderError("the first Ogg page carries no packet")

    def _page(self, header: bytes) -> None:
        granule = int.from_bytes(header[6:14], "little")
        serial = header[14:18]
        self._skip = sum(header[self._PAGE_HEADER :])
        if self._serial is None:
            if header[4] != 0 or not header[5] & 0x02:
                raise OggHeaderError("the Ogg stream does not start with a first page")
            self._serial = serial
            self._first_body = bytearray()
            return
        if serial != self._serial:
            self._other_stream = True
        elif granule != self._NO_GRANULE:
            self._granule = granule

    def _read_head(self, packet: bytes) -> None:
        if not packet.startswith(b"OpusHead"):
            raise OggHeaderError("the Ogg stream does not hold Opus; Earshot reads Ogg Opus only")
        if len(packet) < self._OPUS_HEAD:
            raise OggHeaderError("the OpusHead packet is too short")
        self.channels = packet[9]
        (self.pre_skip,) = struct.unpack_from("<H", packet, 10)
        if self.channels < 1:
            raise OggHeaderError("the OpusHead describes no audio")
        self.header_complete = True

    def finish(self, total_size: int) -> None:
        if not self.header_complete:
            raise OggHeaderError("the body ends before its Ogg Opus header does")
        on_a_boundary = not self._header and not self._skip and not self._lost
        samples = None if self._granule is None else self._granule - self.pre_skip
        duration = None
        if on_a_boundary and not self._other_stream and samples is not None and samples >= 0:
            duration = _bounded_duration_ms(samples * 1000 // self._OPUS_RATE)
        self._duration_ms = duration
        self._finished = True

    def duration_ms(self) -> int | None:
        if not self._finished:
            raise ValueError("finish() the inspector before reading the duration")
        return self._duration_ms


MediaInspector = WavInspector | FlacInspector | OggInspector


def media_inspector(content_type: str) -> MediaInspector | None:
    """A new header reader for a body of ``content_type``; ``None`` for MP3 and WebM,
    whose bodies are not read."""

    if content_type == WAV_CONTENT_TYPE:
        return WavInspector()
    if content_type == FLAC_CONTENT_TYPE:
        return FlacInspector()
    if content_type == OGG_CONTENT_TYPE:
        return OggInspector()
    return None


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
