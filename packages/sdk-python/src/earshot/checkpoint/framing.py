"""Self-delimiting, integrity-checked frames for the append-only journal.

This module is pure: no filesystem, no clocks, no configuration. Framing is the
part of crash recovery that has to be right under an arbitrary interruption, so
it is expressed as a total function over bytes that a fast unit test can drive
through every truncation offset without touching a disk.

Layout of one frame::

    SEP(1)   0x1E              record separator, cheap structural check
    SEQ(4)   big-endian uint32 strictly increasing from 1
    LEN(4)   big-endian uint32 body length
    BODY(LEN)                  canonical JSON, or nonce||AES-256-GCM ciphertext
    CRC(4)   big-endian        CRC-32 of BODY

JSON Lines was rejected because a partially flushed line can still be a
syntactically valid JSON document, so a reader cannot tell a complete record
from a truncated one; and because encryption would force base64 inside a
line-oriented file. Length-prefixing plus a trailing checksum buys back exactly
the atomicity the durable spool got from ``os.replace`` without paying for one
``fsync``/rename cycle per record on the voice path.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

SEPARATOR = 0x1E
HEADER_SIZE = 9  # SEP(1) + SEQ(4) + LEN(4)
CHECKSUM_SIZE = 4
FRAME_OVERHEAD = HEADER_SIZE + CHECKSUM_SIZE
MAX_SEQUENCE = 0xFFFFFFFF

_HEADER = struct.Struct(">BII")
_CHECKSUM = struct.Struct(">I")

# Why the scan stopped. These are diagnostic codes, never raised: a reader that
# raised on damage would turn "some evidence survived" into "no evidence".
STOP_TRUNCATED_HEADER = "truncated_header"
STOP_TRUNCATED_BODY = "truncated_body"
STOP_BAD_SEPARATOR = "bad_separator"
STOP_BAD_CHECKSUM = "bad_checksum"
STOP_SEQUENCE_GAP = "sequence_gap"
STOP_FRAME_TOO_LARGE = "frame_too_large"
STOP_UNREADABLE_BODY = "unreadable_body"


class FrameTooLargeError(ValueError):
    """Raised by :func:`encode_frame` when a body exceeds the configured cap."""


@dataclass(frozen=True)
class Frame:
    sequence: int
    body: bytes


@dataclass(frozen=True)
class FrameScan:
    """The maximal intact prefix of a journal, plus what stopped the scan.

    ``torn_tail_bytes`` is the exact number of trailing bytes that could not be
    interpreted. It is reported rather than hidden because "evidence stops here
    and I know how much I lost" is a materially different claim from "evidence
    stops here".
    """

    frames: tuple[Frame, ...]
    consumed_bytes: int
    torn_tail_bytes: int
    stop_reason: str | None

    @property
    def last_sequence(self) -> int:
        return self.frames[-1].sequence if self.frames else 0


def encode_frame(sequence: int, body: bytes, *, max_body_bytes: int) -> bytes:
    """Encode one frame, or refuse when the body exceeds ``max_body_bytes``."""

    if not 1 <= sequence <= MAX_SEQUENCE:
        raise ValueError("journal sequence numbers start at 1 and are uint32")
    if len(body) > max_body_bytes:
        raise FrameTooLargeError("journal frame body exceeds the configured maximum")
    return b"".join(
        (
            _HEADER.pack(SEPARATOR, sequence, len(body)),
            body,
            _CHECKSUM.pack(zlib.crc32(body) & 0xFFFFFFFF),
        )
    )


def scan_frames(
    data: bytes,
    *,
    max_body_bytes: int,
    open_body: Callable[[int, bytes], bytes | None] | None = None,
    first_sequence: int = 1,
) -> FrameScan:
    """Return the maximal intact prefix of ``data``. Never raises.

    The scan stops at the first frame that is short, mis-separated, fails its
    CRC, jumps a sequence number, or that ``open_body`` refuses (which is how
    AEAD authentication participates). It never resynchronizes by hunting for
    the next separator: a record's own bytes can contain 0x1E, so resynchronizing
    would risk admitting a frame forged out of the middle of a real one. It also
    never truncates or rewrites the input.

    ``open_body`` transforms a stored body into the plaintext the caller wants
    (identity for a plaintext journal, decrypt-and-authenticate for an encrypted
    one) and returns ``None`` to reject the frame.
    """

    frames: list[Frame] = []
    offset = 0
    total = len(data)
    expected_sequence = first_sequence
    stop_reason: str | None = None
    while offset < total:
        if total - offset < HEADER_SIZE:
            stop_reason = STOP_TRUNCATED_HEADER
            break
        separator, sequence, length = _HEADER.unpack_from(data, offset)
        if separator != SEPARATOR:
            stop_reason = STOP_BAD_SEPARATOR
            break
        if length > max_body_bytes:
            # A plausible-looking length beyond the cap is damage, not a record:
            # honoring it would let a corrupt header steer an unbounded read.
            stop_reason = STOP_FRAME_TOO_LARGE
            break
        end = offset + HEADER_SIZE + length + CHECKSUM_SIZE
        if end > total:
            stop_reason = STOP_TRUNCATED_BODY
            break
        body = data[offset + HEADER_SIZE : offset + HEADER_SIZE + length]
        (checksum,) = _CHECKSUM.unpack_from(data, offset + HEADER_SIZE + length)
        if checksum != (zlib.crc32(body) & 0xFFFFFFFF):
            stop_reason = STOP_BAD_CHECKSUM
            break
        if sequence != expected_sequence:
            stop_reason = STOP_SEQUENCE_GAP
            break
        if open_body is not None:
            opened = open_body(sequence, body)
            if opened is None:
                stop_reason = STOP_UNREADABLE_BODY
                break
            body = opened
        frames.append(Frame(sequence=sequence, body=body))
        offset = end
        expected_sequence += 1
    return FrameScan(
        frames=tuple(frames),
        consumed_bytes=offset,
        torn_tail_bytes=total - offset,
        stop_reason=stop_reason,
    )
