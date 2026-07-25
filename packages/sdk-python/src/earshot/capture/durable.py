"""Durable, restart-survivable state for a continuous browser call.

A ``captureVersion: 2`` call lives on the backend and nowhere else: the browser
drains its telemetry and forgets it, so the *only* copy of an in-flight call is
the one the server accumulates. Held purely in memory, a backend restart loses
every in-flight call and orphans nothing recoverable. This module is what makes
such a call survive a restart, reusing the append-only checkpoint journal rather
than inventing a second durable format.

Two things must persist, and they are different in kind:

* **The admitted facts.** These are the journal frames the live session already
  frames and retains; making them durable is a matter of writing those exact
  bytes to an append-only ``.eck`` file as they are retained (see
  :mod:`earshot.live`). On restart the file replays back into a live session
  through the same reader the assembler uses, so the rebuilt session is
  byte-for-byte what it held before -- tailable, sealable, and **provisional**,
  because no close is ever fabricated by a rebuild.

* **The drain-sequencing ledger.** The drain sequence, the ring of accepted-drain
  digests, the observed browser-time span, whether any evidence was lost, and
  whether an explicit ``endCall()`` finalized the call are *not* in the journal:
  the journal holds projected facts, not the drain metadata that decides whether
  the next drain is a fresh extension, an idempotent replay, or a forged rewrite.
  That ledger is this module's :class:`CaptureSidecar`, written next to the
  journal and rewritten atomically after every applied drain.

The sidecar is written **before** the drain's frames, never after, so a crash in
the sub-millisecond window between the two can only leave the sidecar knowing a
drain the journal does not yet carry -- which resumes as a harmless replay, never
as duplicated or fabricated evidence. The rebuilt call re-derives nothing it
cannot observe: the WebRTC carry (the raw snapshot the boundary delta differences
against) is genuinely absent from the journal, so it is dropped and the boundary
across the restart is declared ``carry_lost_on_restart`` coverage rather than
guessed at.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..checkpoint.reader import JournalReader, JournalUnreadableError
from ..checkpoint.records import JournalRecordEntry
from ..contract import Coverage

# The durable artefacts share one stem so a rebuild can pair them: ``<stem>.eck``
# is the journal frames, ``<stem>.capture.json`` the drain-sequencing ledger. The
# stem is a hash of the call key, so the filename never carries a caller
# identifier, exactly as the crash journal's does not.
JOURNAL_SUFFIX = ".eck"
SIDECAR_SUFFIX = ".capture.json"


def capture_journal_stem(call_key: str) -> str:
    """A caller-identifier-free filename stem shared by a call's durable files."""

    digest = hashlib.sha256(
        b"earshot.capture.durable:" + call_key.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return digest[:32]


def journal_path(directory: Path, call_key: str) -> Path:
    return Path(directory) / f"{capture_journal_stem(call_key)}{JOURNAL_SUFFIX}"


def sidecar_path(directory: Path, call_key: str) -> Path:
    return Path(directory) / f"{capture_journal_stem(call_key)}{SIDECAR_SUFFIX}"


@dataclass(frozen=True)
class CaptureSidecar:
    """The drain-sequencing ledger of one call, durable across a restart.

    Everything here is metadata *about the drains*, never their raw content: the
    highest applied drain, a bounded ring of accepted-drain content digests (so a
    resent drain replays and a forged one at a taken slot conflicts), the observed
    browser-time span, the recorder's fact-id counter (so facts minted after the
    restart never collide with those minted before), whether any evidence was
    lost, and whether an explicit close finalized the call.
    """

    project_id: str
    call_key: str
    applied: int
    digests: dict[int, str]
    turn_sequence: int
    first_observed_ms: float | None
    last_observed_ms: float | None
    lossy: bool
    finalized: bool

    def to_json(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "call_key": self.call_key,
            "applied": self.applied,
            "digests": {str(sequence): digest for sequence, digest in self.digests.items()},
            "turn_sequence": self.turn_sequence,
            "first_observed_ms": self.first_observed_ms,
            "last_observed_ms": self.last_observed_ms,
            "lossy": self.lossy,
            "finalized": self.finalized,
        }

    @classmethod
    def from_json(cls, document: dict[str, object]) -> CaptureSidecar:
        raw_digests = document.get("digests") or {}
        digests: dict[int, str] = {}
        if isinstance(raw_digests, dict):
            for sequence, digest in raw_digests.items():
                if isinstance(digest, str):
                    with contextlib.suppress(ValueError):
                        digests[int(sequence)] = digest
        return cls(
            project_id=str(document["project_id"]),
            call_key=str(document["call_key"]),
            applied=int(document["applied"]),  # type: ignore[arg-type]
            digests=digests,
            turn_sequence=int(document.get("turn_sequence", 0)),  # type: ignore[arg-type]
            first_observed_ms=_optional_float(document.get("first_observed_ms")),
            last_observed_ms=_optional_float(document.get("last_observed_ms")),
            lossy=bool(document.get("lossy", False)),
            finalized=bool(document.get("finalized", False)),
        )


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def write_sidecar(directory: Path, sidecar: CaptureSidecar) -> None:
    """Persist the ledger atomically: whole prior value or whole new one, never torn.

    Written to a temporary file, fsynced, then renamed over the target, so a
    reader (and a rebuild) only ever sees a complete ledger. Never raises: a
    ledger the disk refused just means the last applied drain replays on resume,
    which the sequencing already treats as a no-op.
    """

    target = sidecar_path(directory, sidecar.call_key)
    temporary = target.with_name(target.name + ".tmp")
    payload = json.dumps(sidecar.to_json(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def read_sidecar(path: Path) -> CaptureSidecar | None:
    """Load one ledger, or ``None`` when it is missing or unreadable."""

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    try:
        return CaptureSidecar.from_json(document)
    except (KeyError, ValueError, TypeError):
        return None


def iter_sidecars(directory: Path) -> Iterator[Path]:
    """Every durable capture ledger in a directory, in stable order."""

    return iter(sorted(Path(directory).glob(f"*{SIDECAR_SUFFIX}")))


def max_fact_sequence(path: Path, *, key: bytes | None = None) -> int:
    """The highest recorder fact-id counter already spent in a durable journal.

    A fact's synthetic id ends in the recorder's monotonic per-turn counter
    (``event-0-7``, ``quality-0-7``, ``operation-name-0-7``). A restarted recorder
    must resume its counter past every id the journal already holds, or a fact
    minted after the restart would reuse an id the journal already spent and the
    sealed artifact would carry two facts under one id. This reads that ceiling
    straight from the frames on disk, so it is always consistent with the journal's
    actual content regardless of where a crash fell. ``0`` when nothing was read.
    """

    try:
        replay = JournalReader(path, key=key).read()
    except (JournalUnreadableError, OSError):
        return 0
    highest = 0
    for entry in replay.entries:
        if not isinstance(entry, JournalRecordEntry) or entry.value is None:
            continue
        for field in ("event_id", "sample_id", "operation_id"):
            identifier = entry.value.get(field)
            if not isinstance(identifier, str):
                continue
            _, _, tail = identifier.rpartition("-")
            if tail.isdigit():
                highest = max(highest, int(tail))
    return highest


def replay_coverage(path: Path, *, key: bytes | None = None) -> list[Coverage] | None:
    """Rebuild the recorder's coverage list from a durable journal.

    A restarted recorder must resume its coverage supersession from exactly the
    list it held before, or a later in-place supersession would rewrite the wrong
    slot when the sealed artifact is assembled. Coverage is the only journal kind
    that supersedes, and this replays those appends and replacements in journal
    order the same way the assembler does, so the rebuilt recorder's indices line
    up with the frames already on disk. ``None`` when the journal cannot be read.
    """

    try:
        replay = JournalReader(path, key=key).read()
    except (JournalUnreadableError, OSError):
        return None
    coverage: list[Coverage] = []
    for entry in replay.entries:
        if not isinstance(entry, JournalRecordEntry) or entry.kind != "coverage":
            continue
        if entry.value is None:
            continue
        try:
            record = Coverage.model_validate(entry.value)
        except ValueError:
            continue
        if entry.replaces_index is not None and 0 <= entry.replaces_index < len(coverage):
            coverage[entry.replaces_index] = record
        else:
            coverage.append(record)
    return coverage


__all__ = [
    "JOURNAL_SUFFIX",
    "SIDECAR_SUFFIX",
    "CaptureSidecar",
    "capture_journal_stem",
    "iter_sidecars",
    "journal_path",
    "max_fact_sequence",
    "read_sidecar",
    "replay_coverage",
    "sidecar_path",
    "write_sidecar",
]
