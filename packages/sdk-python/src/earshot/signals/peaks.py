"""audiowaveform ``.dat`` version 2 peaks files: the header, reading, and pairing.

The format (https://github.com/bbc/audiowaveform/blob/master/doc/DataFormat.md):
a 24-byte little-endian header ``version (int32) = 2, flags (uint32; bit 0 set =
8-bit values), sample_rate (int32), samples_per_pixel (int32), length (uint32,
pixels), channels (int32)``, then per pixel each channel's minimum and maximum.

The analysis writes 8-bit files at 256 source samples per pixel. A mono pair's
stereo rendition is shown with the two recordings' peaks side by side
(``interleave``), channel 0 user and channel 1 agent, as its audio is.

This module imports only the standard library: the server reads peaks without the
``signals`` extra.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = struct.Struct("<iIiiIi")
VERSION = 2
FLAG_8_BIT = 1
SAMPLES_PER_PIXEL = 256
CONTENT_TYPE = "application/octet-stream"


class PeaksFormatError(ValueError):
    """Bytes that are not an 8-bit audiowaveform ``.dat`` version 2 file."""


@dataclass(frozen=True, slots=True)
class PeaksHeader:
    sample_rate: int
    samples_per_pixel: int
    length: int
    channels: int

    def pack(self) -> bytes:
        return HEADER.pack(
            VERSION,
            FLAG_8_BIT,
            self.sample_rate,
            self.samples_per_pixel,
            self.length,
            self.channels,
        )


def read_header(data: bytes) -> PeaksHeader:
    """The header of an 8-bit version 2 file whose body is complete."""

    if len(data) < HEADER.size:
        raise PeaksFormatError("too short for a .dat header")
    version, flags, rate, per_pixel, length, channels = HEADER.unpack_from(data)
    if version != VERSION or flags != FLAG_8_BIT or rate <= 0 or per_pixel <= 0 or channels <= 0:
        raise PeaksFormatError("not an 8-bit audiowaveform .dat version 2 file")
    if len(data) != HEADER.size + 2 * length * channels:
        raise PeaksFormatError("the body does not match the header")
    return PeaksHeader(rate, per_pixel, length, channels)


def interleave(first: bytes, second: bytes) -> bytes:
    """Two mono files of one timeline as one two-channel file (``first`` is channel 0).

    Both must have the same rate and pixel size. The shorter is padded with silent
    pixels (0, 0): a pair on one timeline differs by at most a few pixels.
    """

    a, b = read_header(first), read_header(second)
    if a.channels != 1 or b.channels != 1:
        raise PeaksFormatError("only mono files are paired")
    if (a.sample_rate, a.samples_per_pixel) != (b.sample_rate, b.samples_per_pixel):
        raise PeaksFormatError("the files have different rates or pixel sizes")
    length = max(a.length, b.length)
    body_a = first[HEADER.size :].ljust(2 * length, b"\x00")
    body_b = second[HEADER.size :].ljust(2 * length, b"\x00")
    pairs = bytearray(4 * length)
    pairs[0::4], pairs[1::4] = body_a[0::2], body_a[1::2]
    pairs[2::4], pairs[3::4] = body_b[0::2], body_b[1::2]
    header = PeaksHeader(a.sample_rate, a.samples_per_pixel, length, 2)
    return header.pack() + bytes(pairs)
