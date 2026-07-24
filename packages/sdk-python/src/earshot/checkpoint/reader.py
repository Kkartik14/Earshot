"""Sequential replay of an append-only journal, damage included.

The reader's contract is that it never raises on damage and never repairs it.
It returns the maximal intact prefix and states exactly how many trailing bytes
it could not interpret. A journal is only ever read, never truncated, rewritten,
or resynchronized past a bad frame: a record's own bytes can contain the frame
separator, so hunting for the next one risks admitting a frame forged out of the
middle of a real record.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .framing import FrameScan, scan_frames
from .keys import AT_REST_NONCE_BYTES, import_aesgcm
from .records import (
    JOURNAL_FORMAT_VERSION,
    JournalEntry,
    JournalFinalize,
    JournalFormatError,
    JournalOpen,
    decode_entry,
    journal_frame_aad,
)
from .writer import DEFAULT_MAX_FRAME_BYTES, JOURNAL_SUFFIX

STATE_FINALIZED = "finalized"
STATE_OPEN = "open"
STATE_UNREADABLE = "unreadable"


class JournalUnreadableError(ValueError):
    """Raised only when a journal has no interpretable header at all.

    Everything past the header degrades to a torn tail. A missing or
    undecryptable header is different in kind: without it, nothing after it can
    be interpreted, so there is no prefix to be authoritative about.
    """


@dataclass(frozen=True)
class JournalReplay:
    """One journal's readable content plus the honest edges of that reading."""

    path: Path
    header: JournalOpen
    entries: tuple[JournalEntry, ...]
    last_sequence: int
    torn_tail_bytes: int
    stop_reason: str | None
    total_bytes: int

    @property
    def finalize(self) -> JournalFinalize | None:
        last = self.entries[-1] if self.entries else None
        return last if isinstance(last, JournalFinalize) else None

    @property
    def close_observed(self) -> bool:
        return self.finalize is not None


@dataclass(frozen=True)
class JournalSummary:
    """What ``earshot checkpoints list`` can say without replaying records."""

    path: Path
    journal_id: str | None
    session_id: str | None
    bundle_id: str | None
    state: str
    last_sequence: int
    torn_tail_bytes: int
    total_bytes: int


class JournalReader:
    """Read one journal file, decrypting it when a key is configured."""

    def __init__(
        self,
        path: Path | str,
        *,
        key: bytes | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self.path = Path(path)
        self._max_frame_bytes = max_frame_bytes
        self._cipher = None if key is None else import_aesgcm()(key)

    def read(self) -> JournalReplay:
        """Replay the journal, stopping at the first unreadable frame."""

        data = self.path.read_bytes()
        decoded: list[JournalEntry] = []
        journal_id: str | None = None

        def open_body(sequence: int, body: bytes) -> bytes | None:
            nonlocal journal_id
            plaintext = self._decrypt(sequence, body, journal_id)
            if plaintext is None:
                return None
            try:
                entry = decode_entry(plaintext)
            except JournalFormatError:
                return None
            if sequence == 1:
                if not isinstance(entry, JournalOpen):
                    return None
                if entry.journal_format_version != JOURNAL_FORMAT_VERSION:
                    return None
                journal_id = entry.journal_id
            elif isinstance(entry, JournalOpen):
                # A second header would mean two sessions spliced into one file.
                return None
            decoded.append(entry)
            return plaintext

        scan = scan_frames(
            data,
            max_body_bytes=self._max_frame_bytes,
            open_body=open_body,
        )
        if not decoded or not isinstance(decoded[0], JournalOpen):
            raise JournalUnreadableError("journal has no readable header")
        header = decoded[0]
        entries = tuple(decoded[1:])
        # Frames after a finalize would mean the writer kept going past close.
        entries = _stop_after_finalize(entries)
        return JournalReplay(
            path=self.path,
            header=header,
            entries=entries,
            last_sequence=scan.last_sequence,
            torn_tail_bytes=scan.torn_tail_bytes,
            stop_reason=scan.stop_reason,
            total_bytes=len(data),
        )

    def summarize(self) -> JournalSummary:
        """Identify the journal without trusting or replaying its records."""

        try:
            replay = self.read()
        except (JournalUnreadableError, OSError):
            size = self.path.stat().st_size if self.path.is_file() else 0
            return JournalSummary(
                path=self.path,
                journal_id=None,
                session_id=None,
                bundle_id=None,
                state=STATE_UNREADABLE,
                last_sequence=0,
                torn_tail_bytes=size,
                total_bytes=size,
            )
        return JournalSummary(
            path=replay.path,
            journal_id=replay.header.journal_id,
            session_id=replay.header.session_id,
            bundle_id=replay.header.bundle_id,
            state=STATE_FINALIZED if replay.close_observed else STATE_OPEN,
            last_sequence=replay.last_sequence,
            torn_tail_bytes=replay.torn_tail_bytes,
            total_bytes=replay.total_bytes,
        )

    def _decrypt(self, sequence: int, body: bytes, journal_id: str | None) -> bytes | None:
        if self._cipher is None:
            return body
        if len(body) <= AT_REST_NONCE_BYTES:
            return None
        if sequence > 1 and journal_id is None:
            return None
        aad = journal_frame_aad(None if sequence == 1 else journal_id, sequence)
        try:
            return bytes(
                self._cipher.decrypt(body[:AT_REST_NONCE_BYTES], body[AT_REST_NONCE_BYTES:], aad)
            )
        except Exception:
            return None


def _stop_after_finalize(entries: tuple[JournalEntry, ...]) -> tuple[JournalEntry, ...]:
    for index, entry in enumerate(entries):
        if isinstance(entry, JournalFinalize):
            return entries[: index + 1]
    return entries


def iter_journals(directory: Path | str) -> Iterator[Path]:
    """Yield every journal file in a checkpoint directory, in stable order."""

    return iter(sorted(Path(directory).glob(f"*{JOURNAL_SUFFIX}")))


def summarize_directory(
    directory: Path | str,
    *,
    key: bytes | None = None,
) -> tuple[JournalSummary, ...]:
    """Identify every journal in a directory without replaying its records."""

    return tuple(JournalReader(path, key=key).summarize() for path in iter_journals(directory))


__all__ = [
    "FrameScan",
    "JournalReader",
    "JournalReplay",
    "JournalSummary",
    "JournalUnreadableError",
    "iter_journals",
    "summarize_directory",
]
