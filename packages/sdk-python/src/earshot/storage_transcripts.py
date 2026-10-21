"""Uploaded transcripts in the catalog: a recording's transcript as evidence of its call (FR-5).

A ``TranscriptStore`` is the storage half of ``POST /v1/recordings/{id}/transcript``.
It runs on an ``IncidentStore``'s database, write locks and clock, and uses the
``CallStore`` for everything a call is (spans, the FR-6 close rules).

**One mutation.** ``attach`` reads the recording and the project's preset, decides,
and writes under one write lock, so two uploads for one recording, however they
interleave, store one transcript:

=========================================  ===============================================
state                                      outcome
=========================================  ===============================================
no such recording (in this project)        ``RecordingNotFoundError`` (404)
the preset is not ``full``                 ``PresetForbidsTranscriptError`` (422):
                                           ``metadata_only`` keeps no transcript text, so
                                           a 2xx would store nothing
no transcript yet                          its spans are written (``created``)
the same transcript (same digest)          nothing is written (an idempotent repeat)
another transcript                         ``TranscriptExistsError`` (409): the first is
                                           kept and never replaced
=========================================  ===============================================

**Binding.** The transcript's spans (``transcripts.transcript_spans``) are on one
trace per recording, which is bound to the recording's call before they are
recorded: the recording names its call exactly, so the spans need no session key
(a call first seen under ``metadata_only`` stores none). They are then recorded as
any evidence is (``CallStore.record_spans_in``): the call is marked dirty with its
due time recomputed, and a closed call gets a later revision.

**The same transcript** is decided by the stored ``earshot.upload.transcript`` span:
its payload carries the transcript's digest, so its stored payload digest equals the
new one exactly when the lines are the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .call_capture import (
    CAPTURE_PRESET_FULL,
    RequestMemo,
    apply_capture_preset,
    payload_digest,
    span_payload,
)
from .storage import StorageError
from .storage_recordings import RecordingError, RecordingNotFoundError, RecordingStore
from .transcripts import header_span_id, transcript_spans

if TYPE_CHECKING:
    from .storage import IncidentStore
    from .storage_calls import CallStore
    from .storage_recordings import Recording
    from .transcripts import Transcript


class PresetForbidsTranscriptError(RecordingError):
    status_code = 422
    code = "EARSHOT_PRESET_FORBIDS_TRANSCRIPT"
    message = "the project's capture preset keeps no transcript text"


class TranscriptExistsError(RecordingError):
    status_code = 409
    code = "EARSHOT_TRANSCRIPT_EXISTS"
    message = "the recording has a different transcript; the first is kept, never replaced"


@dataclass(frozen=True, slots=True)
class AttachedTranscript:
    """The recording a transcript was attached to, and whether this call wrote it."""

    recording: Recording
    created: bool


class TranscriptStore:
    """Transcript storage operations on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore, *, calls: CallStore) -> None:
        self.store = store
        self.calls = calls

    def attach(
        self,
        project_id: str,
        recording_id: str,
        transcript: Transcript,
        *,
        budget_s: float | None = None,
    ) -> AttachedTranscript:
        """Record ``transcript`` as the recording's transcript (see the module table).

        Raises ``transcripts.TranscriptFormatError`` when a line's time is past the
        latest storable time (it is read against the recording's ``started_at``).
        """

        with (
            self.store._mutation("attach_transcript", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            preset = self.calls._read_preset(connection, project_id)
            recording = RecordingStore._row(connection, project_id, recording_id)
            if recording is None:
                raise RecordingNotFoundError
            if preset != CAPTURE_PRESET_FULL:
                raise PresetForbidsTranscriptError
            spans = transcript_spans(
                transcript,
                call_id=recording.call_id,
                recording_id=recording_id,
                started_at=int(recording.started_at_unix_nano),
            )
            header = spans[0]
            trace = bytes(header.span.trace_id)
            stored = connection.execute(
                "SELECT digest FROM spans WHERE project_id = ? AND trace_id = ? AND span_id = ?",
                (project_id, trace, header_span_id()),
            ).fetchone()
            if stored is not None:
                kept = apply_capture_preset(preset, header, memo=RequestMemo())
                if bytes(stored["digest"]) == payload_digest(span_payload(kept.span)):
                    return AttachedTranscript(recording, created=False)
                raise TranscriptExistsError
            now = self.store._clock.unix_nano()
            connection.execute(
                "INSERT INTO call_traces(project_id, trace_id, call_id, has_session_span, "
                "first_span_received_unix_nano, last_span_received_unix_nano) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (project_id, trace, recording.call_id, now, now),
            )
            result = self.calls.record_spans_in(connection, project_id, spans, now=now)
            if result.stored_spans != len(spans):  # pragma: no cover - under the write lock
                raise StorageError("an uploaded transcript's spans changed under the write lock")
            connection.commit()
        return AttachedTranscript(recording, created=True)
