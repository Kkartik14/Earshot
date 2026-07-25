"""Durable, append-only journaling of an open conversation, and its recovery.

The durable spool persists *closed* incidents on their way to a destination.
This package persists an *open* session so that a process which never reaches
``close()`` still leaves recoverable evidence behind. Checkpointing is off by
default: an explicit, owner-private directory is the storage opt-in, and the
journal only ever holds capture classes the configured policy already admits, so
enabling it never widens what earshot retains.
"""

from __future__ import annotations

from .assembler import (
    METHOD_CHECKPOINT_JOURNAL,
    REASON_BEFORE_CLOSE,
    AssemblyError,
    AssemblyReport,
    AssemblyResult,
    assemble_incident,
)
from .framing import FrameScan, scan_frames
from .reader import (
    JournalReader,
    JournalReplay,
    JournalSummary,
    JournalUnreadableError,
    summarize_directory,
)
from .records import JOURNAL_FORMAT_VERSION
from .uploader import CheckpointUploader, UploaderStatus
from .writer import (
    CheckpointConfig,
    CheckpointStatus,
    CheckpointWriter,
    NullCheckpointWriter,
    RecordMutation,
)

__all__ = [
    "JOURNAL_FORMAT_VERSION",
    "METHOD_CHECKPOINT_JOURNAL",
    "REASON_BEFORE_CLOSE",
    "AssemblyError",
    "AssemblyReport",
    "AssemblyResult",
    "CheckpointConfig",
    "CheckpointStatus",
    "CheckpointUploader",
    "CheckpointWriter",
    "FrameScan",
    "JournalReader",
    "JournalReplay",
    "JournalSummary",
    "JournalUnreadableError",
    "NullCheckpointWriter",
    "RecordMutation",
    "UploaderStatus",
    "assemble_incident",
    "scan_frames",
    "summarize_directory",
]
