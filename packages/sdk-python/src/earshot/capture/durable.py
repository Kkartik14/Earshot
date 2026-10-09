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

The sidecar records a pending drain and the journal's prior byte length before
the append. Once every frame is appended and fsynced, the sidecar advances its
committed drain sequence. A restart rolls any still-pending append back to that
byte length and requires the same drain digest to be retried. This covers crashes
before, during, and after the journal append without acknowledging missing facts
or duplicating committed ones. The rebuilt call re-derives nothing it cannot
observe: the WebRTC carry (the raw snapshot the boundary delta differences
against) is genuinely absent from the journal, so it is dropped and the boundary
across the restart is declared ``carry_lost_on_restart`` coverage rather than
guessed at.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import math
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path

from ..checkpoint.reader import JournalReader, JournalUnreadableError
from ..checkpoint.records import JournalRecordEntry
from ..contract import Coverage

try:  # Durable capture is a single-writer journal shared across processes.
    import fcntl
except ImportError:  # pragma: no cover - durable capture requires POSIX file locking
    fcntl = None

DEFAULT_MAX_SEALED_REPLAY_LEDGERS = 1024

# The durable artefacts share one stem so a rebuild can pair them: ``<stem>.eck``
# is the journal frames, ``<stem>.capture.json`` the drain-sequencing ledger. The
# stem is a hash of the call key, so the filename never carries a caller
# identifier, exactly as the crash journal's does not.
JOURNAL_SUFFIX = ".eck"
SIDECAR_SUFFIX = ".capture.json"
WRITER_LOCK_NAME = ".earshot-capture-writer.lock"
DIGEST_RING_SIZE = 64
LEGACY_CAPTURE_DIGEST_VERSION = 1
REJECTION_COVERAGE_CAPTURE_DIGEST_VERSION = 2
CURRENT_CAPTURE_DIGEST_VERSION = 3


def acquire_writer_lock(directory: Path, *, create: bool = False) -> int:
    """Hold an exclusive process lease on one durable capture directory."""

    directory = Path(directory)
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_stat = directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("capture journal path is not a real directory")
    directory.chmod(0o700)
    if fcntl is None:
        raise OSError("durable capture requires POSIX process file locking")

    lock_path = directory / WRITER_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("capture writer lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError(
                    "capture journal directory already has an active writer"
                ) from error
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def release_writer_lock(descriptor: int) -> None:
    """Release a capture directory process lease."""

    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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
    # Stable proof that the per-instance key used to derive hosted call IDs is
    # the same one that was present when this journal was accepted.
    correlation_key_id: str | None = None
    # Set only after IncidentStore durably acknowledges the sealed artifact.
    # A finalized drain alone does not prove that its journal became an artifact.
    sealed: bool = False
    pending_sequence: int | None = None
    pending_digest: str | None = None
    pending_journal_size: int | None = None
    retry_sequence: int | None = None
    retry_digest: str | None = None
    digest_versions: dict[int, int] = dataclass_field(default_factory=dict)
    pending_digest_version: int | None = None
    retry_digest_version: int | None = None
    # Expired sessions stay owned and retryable on disk, but are not reattached
    # to the bounded live-session registry until a client retries that call.
    expired: bool = False
    # Current call metadata is persisted independently from per-drain digests so
    # a resumed call cannot silently switch its trace context or clock domain's
    # uncertainty. Older sidecars leave this unbound for compatibility.
    call_metadata_bound: bool = False
    call_metadata_digest: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "call_key": self.call_key,
            "applied": self.applied,
            "digests": {str(sequence): digest for sequence, digest in self.digests.items()},
            "digest_versions": {
                str(sequence): self.digest_versions.get(sequence, CURRENT_CAPTURE_DIGEST_VERSION)
                for sequence in self.digests
            },
            "turn_sequence": self.turn_sequence,
            "first_observed_ms": self.first_observed_ms,
            "last_observed_ms": self.last_observed_ms,
            "lossy": self.lossy,
            "finalized": self.finalized,
            "correlation_key_id": self.correlation_key_id,
            "sealed": self.sealed,
            "expired": self.expired,
            "call_metadata_bound": self.call_metadata_bound,
            "call_metadata_digest": self.call_metadata_digest,
            "pending_sequence": self.pending_sequence,
            "pending_digest": self.pending_digest,
            "pending_journal_size": self.pending_journal_size,
            "pending_digest_version": (
                self.pending_digest_version
                if self.pending_sequence is None
                else self.pending_digest_version or LEGACY_CAPTURE_DIGEST_VERSION
            ),
            "retry_sequence": self.retry_sequence,
            "retry_digest": self.retry_digest,
            "retry_digest_version": (
                self.retry_digest_version
                if self.retry_sequence is None
                else self.retry_digest_version or LEGACY_CAPTURE_DIGEST_VERSION
            ),
        }

    @classmethod
    def from_json(cls, document: dict[str, object]) -> CaptureSidecar:
        project_id = document.get("project_id")
        call_key = document.get("call_key")
        applied = _required_nonnegative_int(document.get("applied"), "applied")
        turn_sequence = _required_nonnegative_int(document.get("turn_sequence", 0), "turn_sequence")
        lossy = _required_bool(document.get("lossy", False), "lossy")
        finalized = _required_bool(document.get("finalized", False), "finalized")
        sealed = _required_bool(document.get("sealed", False), "sealed")
        expired = _required_bool(document.get("expired", False), "expired")
        call_metadata_bound = _required_bool(
            document.get("call_metadata_bound", False), "call_metadata_bound"
        )
        call_metadata_digest = _optional_sha256(document.get("call_metadata_digest"))
        correlation_key_id = _optional_sha256(document.get("correlation_key_id"))
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("capture sidecar project identity is invalid")
        if not isinstance(call_key, str) or not call_key:
            raise ValueError("capture sidecar call identity is invalid")

        pending_sequence = _optional_nonnegative_int(document.get("pending_sequence"))
        pending_journal_size = _optional_nonnegative_int(document.get("pending_journal_size"))
        pending_digest = _optional_sha256(document.get("pending_digest"))
        retry_sequence = _optional_nonnegative_int(document.get("retry_sequence"))
        retry_digest = _optional_sha256(document.get("retry_digest"))
        pending_digest_version = _optional_capture_digest_version(
            document.get("pending_digest_version"),
            default=(LEGACY_CAPTURE_DIGEST_VERSION if pending_sequence is not None else None),
        )
        retry_digest_version = _optional_capture_digest_version(
            document.get("retry_digest_version"),
            default=(LEGACY_CAPTURE_DIGEST_VERSION if retry_sequence is not None else None),
        )
        if any(
            value is not None for value in (pending_sequence, pending_digest, pending_journal_size)
        ) and (
            pending_sequence is None
            or pending_digest is None
            or pending_journal_size is None
            or pending_sequence < 1
        ):
            raise ValueError("capture sidecar pending transaction is incomplete")
        if (retry_sequence is None) != (retry_digest is None):
            raise ValueError("capture sidecar retry identity is incomplete")
        if (pending_sequence is None) != (pending_digest_version is None):
            raise ValueError("capture sidecar pending digest version is incomplete")
        if (retry_sequence is None) != (retry_digest_version is None):
            raise ValueError("capture sidecar retry digest version is incomplete")
        if retry_sequence is not None and retry_sequence < 1:
            raise ValueError("capture sidecar retry sequence is invalid")
        if pending_sequence is not None and pending_sequence <= applied:
            raise ValueError("capture sidecar pending sequence is already applied")
        if retry_sequence is not None and retry_sequence <= applied:
            raise ValueError("capture sidecar retry sequence is already applied")
        if (
            pending_sequence is not None
            and retry_sequence is not None
            and (
                pending_sequence != retry_sequence
                or pending_digest != retry_digest
                or pending_digest_version != retry_digest_version
            )
        ):
            raise ValueError("capture sidecar pending and retry identities disagree")
        if sealed and (not finalized or pending_sequence is not None or retry_sequence is not None):
            raise ValueError("capture sidecar sealed state is inconsistent")
        if expired and (sealed or finalized):
            raise ValueError("capture sidecar cannot be expired after finalization")
        if call_metadata_bound:
            if call_metadata_digest is None:
                raise ValueError("capture sidecar call metadata is incomplete")
        elif call_metadata_digest is not None:
            raise ValueError("unbound capture sidecar call metadata must be empty")
        if finalized and (pending_sequence is not None or retry_sequence is not None):
            raise ValueError("capture sidecar finalized state has an unresolved drain")

        raw_digests = document.get("digests", {})
        if not isinstance(raw_digests, dict):
            raise ValueError("capture sidecar digests must be an object")
        digests: dict[int, str] = {}
        for sequence, digest in raw_digests.items():
            if (
                not isinstance(sequence, str)
                or not sequence.isascii()
                or not sequence.isdecimal()
                or str(int(sequence)) != sequence
            ):
                raise ValueError("capture sidecar digest sequence is invalid")
            parsed_sequence = int(sequence)
            if parsed_sequence < 1 or parsed_sequence > applied:
                raise ValueError("capture sidecar digest sequence is outside applied state")
            parsed_digest = _optional_sha256(digest)
            if parsed_digest is None:
                raise ValueError("capture sidecar digest is missing")
            digests[parsed_sequence] = parsed_digest
        if len(digests) > DIGEST_RING_SIZE:
            raise ValueError("capture sidecar digest ring exceeds its bound")
        if (applied == 0 and digests) or (applied > 0 and applied not in digests):
            raise ValueError("capture sidecar digest ring does not cover its latest drain")

        raw_versions = document.get("digest_versions")
        if raw_versions is None:
            digest_versions = {sequence: LEGACY_CAPTURE_DIGEST_VERSION for sequence in digests}
        else:
            if not isinstance(raw_versions, dict):
                raise ValueError("capture sidecar digest versions must be an object")
            digest_versions: dict[int, int] = {}
            for sequence, version in raw_versions.items():
                if (
                    not isinstance(sequence, str)
                    or not sequence.isascii()
                    or not sequence.isdecimal()
                    or str(int(sequence)) != sequence
                ):
                    raise ValueError("capture sidecar digest version sequence is invalid")
                parsed_sequence = int(sequence)
                parsed_version = _optional_capture_digest_version(version, default=None)
                if parsed_version is None or parsed_sequence not in digests:
                    raise ValueError("capture sidecar digest version is invalid")
                digest_versions[parsed_sequence] = parsed_version
            if digest_versions.keys() != digests.keys():
                raise ValueError("capture sidecar digest versions do not match its digests")

        first_observed_ms = _optional_finite_float(document.get("first_observed_ms"))
        last_observed_ms = _optional_finite_float(document.get("last_observed_ms"))
        if (first_observed_ms is None) != (last_observed_ms is None):
            raise ValueError("capture sidecar observation span is incomplete")
        if (
            first_observed_ms is not None
            and last_observed_ms is not None
            and first_observed_ms > last_observed_ms
        ):
            raise ValueError("capture sidecar observation span is reversed")
        return cls(
            project_id=project_id,
            call_key=call_key,
            applied=applied,
            digests=digests,
            turn_sequence=turn_sequence,
            first_observed_ms=first_observed_ms,
            last_observed_ms=last_observed_ms,
            lossy=lossy,
            finalized=finalized,
            correlation_key_id=correlation_key_id,
            sealed=sealed,
            expired=expired,
            call_metadata_bound=call_metadata_bound,
            call_metadata_digest=call_metadata_digest,
            pending_sequence=pending_sequence,
            pending_digest=pending_digest,
            pending_journal_size=pending_journal_size,
            digest_versions=digest_versions,
            pending_digest_version=pending_digest_version,
            retry_sequence=retry_sequence,
            retry_digest=retry_digest,
            retry_digest_version=retry_digest_version,
        )


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("capture sidecar sequence or offset is invalid")
    return value


def _required_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"capture sidecar {name} is invalid")
    return value


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"capture sidecar {name} is invalid")
    return value


def _optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("capture sidecar digest is invalid")
    return value


def _optional_capture_digest_version(value: object, *, default: int | None) -> int | None:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value
        not in {
            LEGACY_CAPTURE_DIGEST_VERSION,
            REJECTION_COVERAGE_CAPTURE_DIGEST_VERSION,
            CURRENT_CAPTURE_DIGEST_VERSION,
        }
    ):
        raise ValueError("capture sidecar digest version is unsupported")
    return value


def recover_pending_sidecar(directory: Path, sidecar: CaptureSidecar) -> CaptureSidecar:
    """Undo an uncommitted append and retain its identity for an exact retry.

    The intent sidecar is the durable write-ahead record. A process restart must
    truncate the corresponding journal before exposing it to the live registry;
    even a complete append is rolled back when its commit marker was not durable.
    The client still owns the unacknowledged request and retries its same digest.
    """

    if sidecar.pending_sequence is None:
        return sidecar
    assert sidecar.pending_journal_size is not None
    assert sidecar.pending_digest is not None
    journal = journal_path(directory, sidecar.call_key)
    existed = False
    try:
        size = journal.stat().st_size
        existed = True
    except FileNotFoundError:
        size = 0
    if size < sidecar.pending_journal_size:
        raise OSError("capture journal is shorter than its committed offset")
    if sidecar.pending_journal_size == 0:
        if existed:
            journal.unlink()
            _fsync_directory(directory)
    elif existed and size > sidecar.pending_journal_size:
        descriptor = os.open(journal, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, sidecar.pending_journal_size)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    recovered = replace(
        sidecar,
        pending_sequence=None,
        pending_digest=None,
        pending_journal_size=None,
        pending_digest_version=None,
        retry_sequence=sidecar.pending_sequence,
        retry_digest=sidecar.pending_digest,
        retry_digest_version=sidecar.pending_digest_version,
    )
    write_sidecar(directory, recovered)
    return recovered


def discard_rejected_first_drain(directory: Path, sidecar: CaptureSidecar) -> None:
    """Remove ownership for a first drain known to have committed no journal bytes."""

    if (
        sidecar.applied != 0
        or sidecar.pending_sequence is not None
        or sidecar.retry_sequence is None
        or sidecar.finalized
        or sidecar.sealed
    ):
        raise OSError("capture sidecar does not describe a rejected first drain")

    ledger = sidecar_path(directory, sidecar.call_key)
    try:
        ledger_stat = ledger.lstat()
    except OSError as error:
        raise OSError("rejected capture ownership ledger cannot be inspected") from error
    if not stat.S_ISREG(ledger_stat.st_mode) or read_sidecar(ledger) != sidecar:
        raise OSError("rejected capture ownership ledger changed unexpectedly")

    journal = journal_path(directory, sidecar.call_key)
    try:
        journal.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise OSError("rejected capture journal cannot be inspected") from error
    else:
        raise OSError("rejected first drain still has journal bytes")

    ledger.unlink()
    _fsync_directory(Path(directory))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _optional_finite_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("capture sidecar observation time is invalid")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("capture sidecar observation time is not finite")
    return numeric


def write_sidecar(directory: Path, sidecar: CaptureSidecar) -> None:
    """Persist the ledger atomically: whole prior value or whole new one, never torn.

    Written to a temporary file, fsynced, then renamed over the target, so a
    reader (and a rebuild) only ever sees a complete ledger. Durable browser-call
    journals require this ledger so recovery and project erasure can attribute
    every journal to one project. A ledger write failure therefore aborts the
    drain before its journal is extended.
    """

    directory = Path(directory)
    target = sidecar_path(directory, sidecar.call_key)
    temporary = target.with_name(target.name + ".tmp")
    payload = json.dumps(sidecar.to_json(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        directory_stat = directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise NotADirectoryError(f"capture journal path is not a directory: {directory}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError("capture sidecar write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        _fsync_directory(directory)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def remove_sealed_journal(directory: Path, call_key: str) -> None:
    """Remove a journal whose durable ledger proves its artifact was stored."""

    path = journal_path(directory, call_key)
    path.unlink(missing_ok=True)
    _fsync_directory(directory)


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


def prune_sealed_sidecars(
    directory: Path,
    *,
    max_count: int,
    protected_call_keys: frozenset[str] = frozenset(),
) -> None:
    """Bound retained sealed replay ledgers, preserving the newest safe entries.

    Active ledgers, unreadable files, and sealed ledgers whose journal still
    exists are preserved. Only a regular, attributable sealed sidecar whose
    journal has already been removed is eligible for retention pruning.
    """

    if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 1:
        raise ValueError("max_count must be a positive integer")

    directory = Path(directory)
    sealed_count = 0
    candidates: list[tuple[int, str, Path]] = []
    for path in iter_sidecars(directory):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        sidecar = read_sidecar(path)
        if (
            sidecar is None
            or not sidecar.sealed
            or sidecar_path(directory, sidecar.call_key) != path
        ):
            continue
        try:
            journal_path(directory, sidecar.call_key).lstat()
        except FileNotFoundError:
            sealed_count += 1
            if sidecar.call_key not in protected_call_keys:
                candidates.append((metadata.st_mtime_ns, path.name, path))
        except OSError:
            continue

    candidates.sort()
    removed = False
    for _, _, path in candidates[: max(0, sealed_count - max_count)]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        path.unlink()
        removed = True
    if removed:
        _fsync_directory(directory)


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
    "DIGEST_RING_SIZE",
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
