"""Append-only crash journal for an open conversation.

The threat this addresses is *process* death — SIGKILL, the OOM killer,
``os._exit`` — not host death. A plain ``write(2)`` into the page cache already
survives process death: the kernel owns the bytes the moment the syscall
returns. ``fsync`` only buys protection against a kernel panic or power loss.
So the design is:

* ``append`` does one ``os.write`` on an ``O_APPEND`` descriptor, called from
  inside the recorder's lock so journal order is exactly admission order;
* ``fsync`` runs on a dedicated daemon thread on an interval and is *never* held
  inside the recorder's lock;
* the ``open``, ``exhausted``, and ``finalize`` frames are always fsynced
  regardless of mode, because losing the header makes everything after it
  uninterpretable and losing the finalize frame silently downgrades a clean
  close to a recovered artifact.

Which yields the honest guarantee: process-level termination loses **zero**
admitted facts; host-level failure loses at most one fsync window.

Every method is total. On any ``OSError`` the writer marks itself degraded,
emits a diagnostic, and stops writing — a journal that kept appending across a
gap would be a lie about ordering. Nothing here raises into a voice callback.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import secrets
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..privacy import CaptureClass, CapturePolicy, Omission
from .framing import FrameTooLargeError, encode_frame
from .keys import (
    AT_REST_NONCE_BYTES,
    import_aesgcm,
    prepare_private_directory,
    resolve_at_rest_key,
)

# Re-exported rather than redefined: every checkpoint size bound is decided in
# one module so the local journal and the wire cannot drift apart.
from .limits import (
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_MAX_JOURNAL_BYTES,
    DEFAULT_MAX_JOURNAL_RECORDS,
)
from .records import (
    JOURNAL_FORMAT_VERSION,
    REASON_JOURNAL_FULL,
    JournalEntry,
    JournalExhausted,
    JournalFinalize,
    JournalLimitEntry,
    JournalOmission,
    JournalOpen,
    JournalOperationOpen,
    JournalRecordEntry,
    encode_entry,
    governance_to_journal,
    journal_frame_aad,
)

JOURNAL_SUFFIX = ".eck"
QUARANTINE_DIRECTORY = "quarantine"

DEFAULT_FSYNC_INTERVAL_MS = 250

FSYNC_MODES = ("interval", "always", "never")

DIAGNOSTIC_WRITE_FAILED = "checkpoint.write_failed"
DIAGNOSTIC_JOURNAL_FULL = "checkpoint.journal_full"


@dataclass(frozen=True)
class CheckpointConfig:
    """Where and how an open conversation is journaled.

    ``checkpoint_dir`` is mandatory and has no default: writing session evidence
    to disk is a storage decision an operator makes explicitly, exactly like the
    durable spool's private directory.
    """

    checkpoint_dir: Path
    destination_fingerprint: str | None = None
    fsync_mode: str = "interval"
    fsync_interval_ms: int = DEFAULT_FSYNC_INTERVAL_MS
    max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES
    max_records: int = DEFAULT_MAX_JOURNAL_RECORDS
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    checkpoint_key: bytes | str | None = None
    keep_finalized: bool = False

    def __post_init__(self) -> None:
        if self.fsync_mode not in FSYNC_MODES:
            raise ValueError("checkpoint fsync_mode must be interval, always, or never")
        for name in ("fsync_interval_ms", "max_journal_bytes", "max_records", "max_frame_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"checkpoint {name} must be a positive integer")


@dataclass(frozen=True)
class CheckpointStatus:
    state: str
    journal_id: str | None
    path: str | None
    last_sequence: int
    journal_bytes: int
    degraded: bool
    journal_complete: bool
    dropped_records: int
    last_failure: str | None


@dataclass(frozen=True)
class RecordMutation:
    """One admitted recorder mutation, ready to be journaled.

    ``omissions`` and ``retained_classes`` are the exact appends the recorder
    made in the same critical section, which is why replaying them reproduces
    the privacy manifest rather than approximating it.
    """

    kind: str
    record: BaseModel | None = None
    omissions: Sequence[Omission] = ()
    retained_classes: Sequence[CaptureClass] = ()
    replaces_index: int | None = None
    raw_payload: bytes | None = None


class NullCheckpointWriter:
    """The no-op journal used whenever checkpointing is not configured."""

    enabled = False

    def open_journal(self, **_kwargs: Any) -> None:
        return None

    def append_record(self, _mutation: RecordMutation) -> None:
        return None

    def append_limit(self, **_kwargs: Any) -> None:
        return None

    def append_operation_open(self, **_kwargs: Any) -> None:
        return None

    def finalize(self, **_kwargs: Any) -> None:
        return None

    def release(self, *, delivered: bool = False) -> None:
        return None

    def status(self) -> CheckpointStatus:
        return CheckpointStatus(
            state="disabled",
            journal_id=None,
            path=None,
            last_sequence=0,
            journal_bytes=0,
            degraded=False,
            journal_complete=True,
            dropped_records=0,
            last_failure=None,
        )


class CheckpointWriter:
    """One append-only journal for one recorder.

    One journal per recorder, one directory per process. This is not a
    coordinated multi-process queue and does not try to be, for the same reason
    the durable spool is not: cross-process ordering would need a protocol that
    buys nothing for a bounded voice session.
    """

    enabled = True

    def __init__(
        self,
        config: CheckpointConfig,
        *,
        diagnostic: Callable[[Any], None] | None = None,
    ) -> None:
        self.config = config
        self._diagnostic = diagnostic
        self._lock = threading.Lock()
        self._directory = Path(config.checkpoint_dir)
        self._route_fingerprint = (
            config.destination_fingerprint or hashlib.sha256(b"earshot.standalone").hexdigest()
        )
        if len(self._route_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self._route_fingerprint
        ):
            raise ValueError("destination_fingerprint must be 64 lowercase hexadecimal characters")
        # Fail closed: a configured key with no ``cryptography`` installed must
        # refuse to construct rather than silently journal plaintext.
        key = resolve_at_rest_key(
            config.checkpoint_key,
            env_var="EARSHOT_CHECKPOINT_KEY",
            env_file_var="EARSHOT_CHECKPOINT_KEY_FILE",
            label="checkpoint key",
            fallback=("EARSHOT_SPOOL_KEY", "EARSHOT_SPOOL_KEY_FILE"),
        )
        if key is None:
            self._cipher = None
        else:
            try:
                aesgcm = import_aesgcm()
            except ImportError as error:
                raise RuntimeError(
                    "checkpoint encryption is configured (checkpoint_key / "
                    "EARSHOT_CHECKPOINT_KEY / EARSHOT_CHECKPOINT_KEY_FILE / the spool key) "
                    "but the 'cryptography' package is not installed; install "
                    "earshot-observability[spool-encryption]"
                ) from error
            self._cipher = aesgcm(key)
        prepare_private_directory(self._directory, label="checkpoint_dir")
        self._journal_id: str | None = None
        self._path: Path | None = None
        self._descriptor: int | None = None
        self._sequence = 0
        self._journal_bytes = 0
        self._degraded = False
        self._journal_complete = True
        self._dropped_records = 0
        self._closed = False
        self._finalized = False
        self._last_failure: str | None = None
        self._pending_fsync = False
        self._stop = threading.Event()
        self._fsync_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ open

    def open_journal(
        self,
        *,
        producer_name: str,
        producer_version: str,
        bundle_id: str,
        session_id: str,
        clock_domain_id: str,
        started_wall: int,
        started_mono: int,
        manual_trace_id: str,
        capture_policy: CapturePolicy,
        max_records: int,
        max_capture_bytes: int,
        max_raw_otlp_bytes: int,
        max_value_bytes: int,
    ) -> None:
        """Write the header frame. Called once, before the first admitted fact."""

        with self._lock:
            if self._journal_id is not None or self._closed:
                return
            journal_id = uuid.uuid4().hex
            digest = hashlib.sha256(bundle_id.encode("utf-8", errors="surrogatepass")).hexdigest()
            # The filename never leaks a caller identifier: the bundle id is
            # hashed and the route fingerprint is already credential-free.
            name = f"{self._route_fingerprint[:16]}-{digest[:32]}{JOURNAL_SUFFIX}"
            path = self._directory / name
            header = JournalOpen(
                journal_format_version=JOURNAL_FORMAT_VERSION,
                journal_id=journal_id,
                producer_name=producer_name,
                producer_version=producer_version,
                bundle_id=bundle_id,
                session_id=session_id,
                clock_domain_id=clock_domain_id,
                started_wall=str(started_wall),
                started_mono=str(started_mono),
                manual_trace_id=manual_trace_id,
                policy_id=capture_policy.policy_id,
                policy_version=capture_policy.policy_version,
                enabled_classes=tuple(
                    sorted(capture_class.value for capture_class in capture_policy.enabled)
                ),
                governance={
                    capture_class.value: governance_to_journal(governance)
                    for capture_class, governance in sorted(
                        capture_policy.governance.items(), key=lambda item: item[0].value
                    )
                },
                max_records=max_records,
                max_capture_bytes=max_capture_bytes,
                max_raw_otlp_bytes=max_raw_otlp_bytes,
                max_value_bytes=max_value_bytes,
            )
            try:
                if path.is_symlink():
                    raise OSError("checkpoint journal path must not be a symbolic link")
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND,
                    0o600,
                )
            except OSError as error:
                self._fail_locked(error)
                return
            self._descriptor = descriptor
            self._path = path
            self._journal_id = journal_id
            if not self._append_locked(header, force_fsync=True):
                return
            if self.config.fsync_mode == "interval":
                self._fsync_thread = threading.Thread(
                    target=self._run_fsync,
                    name="earshot-checkpoint-fsync",
                    daemon=True,
                )
                self._fsync_thread.start()

    # ---------------------------------------------------------------- append

    def append_record(self, mutation: RecordMutation) -> None:
        """Journal one admitted mutation, from inside the recorder's lock."""

        value: dict[str, Any] | None = None
        if mutation.raw_payload is not None:
            # Raw OTLP is opaque bytes, which JSON mode cannot represent at all.
            # They are still journaled, base64-wrapped: they were admitted under
            # the caller's policy, and a journal that dropped them would not
            # replay to the artifact this session is going to produce.
            value = (
                {}
                if mutation.record is None
                else mutation.record.model_dump(mode="json", exclude={"payload"})
            )
            value["payload_base64"] = base64.b64encode(mutation.raw_payload).decode("ascii")
        elif mutation.record is not None:
            value = mutation.record.model_dump(mode="json")
        entry = JournalRecordEntry(
            kind=mutation.kind,  # type: ignore[arg-type]
            value=value,
            omissions=tuple(
                JournalOmission(
                    field_key_sha256=omission.field_key_sha256,
                    capture_class=omission.capture_class.value,
                    reason=omission.reason,
                )
                for omission in mutation.omissions
            ),
            retained_classes=tuple(
                capture_class.value for capture_class in mutation.retained_classes
            ),
            replaces_index=mutation.replaces_index,
        )
        with self._lock:
            self._append_locked(entry)

    def append_limit(
        self,
        *,
        reason: str,
        kind: str,
        capture_class: CaptureClass,
        estimated_bytes: int,
        whole_record: bool,
        freeze: bool,
    ) -> None:
        """Journal one ``_note_omission_locked`` call so replay re-derives it."""

        entry = JournalLimitEntry(
            reason=reason,
            kind=kind,
            capture_class=capture_class.value,
            estimated_bytes=max(0, estimated_bytes),
            whole_record=whole_record,
            freeze=freeze,
        )
        with self._lock:
            self._append_locked(entry)

    def append_operation_open(
        self,
        *,
        operation_id: str,
        operation_name: str,
        operation_name_sha256: str | None,
        started_at: BaseModel,
        participant_id: str | None,
        stream_id: str | None,
        turn_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        parent_span_id: str | None,
        parent_scope: str,
    ) -> None:
        entry = JournalOperationOpen(
            operation_id=operation_id,
            operation_name=operation_name,
            operation_name_sha256=operation_name_sha256,
            started_at=started_at.model_dump(mode="json"),
            participant_id=participant_id,
            stream_id=stream_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            parent_scope=parent_scope,
        )
        with self._lock:
            self._append_locked(entry)

    def finalize(
        self,
        *,
        status: str,
        status_attributes: dict[str, str],
        ended: BaseModel,
        first_limit_reason: str | None,
        truncated_records: int,
        estimated_omitted_bytes: int,
        omitted_records_by_kind: Iterable[tuple[str, int]],
        omitted_records_by_capture_class: Iterable[tuple[str, int]],
        retained_classes: Iterable[CaptureClass],
        record_counts: dict[str, int],
    ) -> None:
        """Declare that the recorder reached ``close()``.

        Written before validation and before export so a crash between close and
        delivery still recovers a byte-identical artifact.
        """

        with self._lock:
            if self._journal_id is None or self._closed or self._finalized:
                return
            entry = JournalFinalize(
                status=status,
                status_attributes=dict(status_attributes),
                ended=ended.model_dump(mode="json"),
                journal_complete=self._journal_complete,
                first_limit_reason=first_limit_reason,
                truncated_records=truncated_records,
                estimated_omitted_bytes=estimated_omitted_bytes,
                omitted_records_by_kind=tuple(omitted_records_by_kind),
                omitted_records_by_capture_class=tuple(omitted_records_by_capture_class),
                retained_classes=tuple(
                    sorted(capture_class.value for capture_class in retained_classes)
                ),
                record_counts=dict(record_counts),
            )
            if self._append_locked(entry, force_fsync=True, bypass_caps=True):
                self._finalized = True

    # --------------------------------------------------------------- release

    def release(self, *, delivered: bool = False) -> None:
        """Stop journaling, and unlink the journal once a successor exists.

        A finalized journal is removed only when the incident has reached a
        durable successor. A crash between finalize and unlink is harmless:
        replaying a finalized journal reproduces the closed artifact byte for
        byte, and content-addressed ingest deduplicates the result. That is why
        finalize-then-unlink is safe — because recovery is deterministic.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            descriptor = self._descriptor
            path = self._path
            self._descriptor = None
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.fsync(descriptor)
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            remove = (
                path is not None
                and self._finalized
                and delivered
                and not self.config.keep_finalized
            )
        thread = self._fsync_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if remove and path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    def status(self) -> CheckpointStatus:
        with self._lock:
            return CheckpointStatus(
                state="closed" if self._closed else "open",
                journal_id=self._journal_id,
                path=None if self._path is None else str(self._path),
                last_sequence=self._sequence,
                journal_bytes=self._journal_bytes,
                degraded=self._degraded,
                journal_complete=self._journal_complete,
                dropped_records=self._dropped_records,
                last_failure=self._last_failure,
            )

    # -------------------------------------------------------------- internal

    def _append_locked(
        self,
        entry: JournalEntry,
        *,
        force_fsync: bool = False,
        bypass_caps: bool = False,
    ) -> bool:
        if self._descriptor is None or self._degraded or self._closed:
            return False
        if not self._journal_complete and not bypass_caps:
            self._dropped_records += 1
            return False
        try:
            body = encode_entry(entry)
        except (TypeError, ValueError) as error:
            # An unencodable entry is a bug, not corruption. Degrade rather than
            # raise: instrumentation must never break the voice loop.
            self._fail_locked(error)
            return False
        sequence = self._sequence + 1
        if self._cipher is not None:
            nonce = secrets.token_bytes(AT_REST_NONCE_BYTES)
            body = nonce + self._cipher.encrypt(nonce, body, self._frame_aad(sequence))
        try:
            frame = encode_frame(sequence, body, max_body_bytes=self.config.max_frame_bytes)
        except (FrameTooLargeError, ValueError):
            self._exhaust_locked()
            return False
        if not bypass_caps and (
            self._journal_bytes + len(frame) > self.config.max_journal_bytes
            or self._sequence >= self.config.max_records
        ):
            self._exhaust_locked()
            return False
        try:
            written = os.write(self._descriptor, frame)
        except OSError as error:
            self._fail_locked(error)
            return False
        if written != len(frame):
            # A short write leaves a torn frame the reader stops at. Stop here so
            # no later frame is ever appended across that gap.
            self._journal_bytes += written
            self._fail_locked(OSError("short journal write"))
            return False
        self._sequence = sequence
        self._journal_bytes += written
        if self.config.fsync_mode == "always" or force_fsync:
            try:
                os.fsync(self._descriptor)
            except OSError as error:
                self._fail_locked(error)
                return False
        else:
            self._pending_fsync = True
        return True

    def _frame_aad(self, sequence: int) -> bytes:
        """Bind format, journal, and position so a frame cannot be moved.

        With the sequence in the AAD a frame cannot be reordered, duplicated at
        another offset, or transplanted into another journal without failing
        authentication. The header binds no journal id because the id is what it
        carries; transplanting a header only makes the journal a different one,
        and every frame after it then fails to authenticate.
        """

        return journal_frame_aad(None if sequence == 1 else self._journal_id, sequence)

    def _exhaust_locked(self) -> None:
        """Record the cap that stopped the journal, then stop accepting facts.

        The terminal marker deliberately bypasses the byte cap. A journal that
        simply stopped would leave a recovered artifact silently short; the
        marker is what turns that into a declared limitation.
        """

        if not self._journal_complete:
            self._dropped_records += 1
            return
        self._journal_complete = False
        self._dropped_records += 1
        self._append_locked(
            JournalExhausted(reason=REASON_JOURNAL_FULL),
            force_fsync=True,
            bypass_caps=True,
        )
        self._notify(DIAGNOSTIC_JOURNAL_FULL)

    def _fail_locked(self, error: BaseException) -> None:
        if self._degraded:
            return
        self._degraded = True
        self._journal_complete = False
        self._last_failure = type(error).__name__
        self._notify(DIAGNOSTIC_WRITE_FAILED)

    def _notify(self, code: str) -> None:
        diagnostic = self._diagnostic
        if diagnostic is None:
            return
        from ..exporter import ExportDiagnostic  # lazy: keeps the import graph acyclic

        with contextlib.suppress(Exception):
            # Diagnostics are advisory; they never break instrumentation.
            diagnostic(ExportDiagnostic(code, self._journal_id or "checkpoint"))

    def _run_fsync(self) -> None:
        interval = self.config.fsync_interval_ms / 1000.0
        while not self._stop.wait(interval):
            with self._lock:
                descriptor = self._descriptor
                pending = self._pending_fsync
                self._pending_fsync = False
            if descriptor is None or not pending:
                continue
            try:
                os.fsync(descriptor)
            except OSError:
                with self._lock:
                    self._fail_locked(OSError("journal fsync failed"))
