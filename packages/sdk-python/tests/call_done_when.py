"""FR-12's done-when checks for one call, shared by the two end-to-end tests.

Both tests drive a real ``earshot serve`` process with one call's evidence: a real
Pipecat call (``apps/ingest/tests/e2e_real``) or the real capture replayed
(``apps/ingest/tests/e2e``). Both then ask the same questions of the server's
HTTP API, its catalog and its blob store:

- the call settles: ``state=closed``, not ``dirty``, no pending recording, and the
  latest revision holds both recordings (hard timeout 120 s);
- exactly one call exists for the session key, and its latest revision closed on
  the end signal and is final;
- the agent said something; the stored recordings are the uploaded bytes, and
  their content is a WAV longer than 1 s;
- every exported span is stored, and no span was rejected, conflicted or parked
  and discarded;
- the recordings' ``started_at`` is within 5 s of the first ``stt`` span.

The catalog is read directly only for what the API does not show (``dirty``, the
stored span rows, the recordings' blob keys); reads take no lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import time
import wave
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otlp_support import Reply, request

SETTLE_TIMEOUT_S = 120.0
ALIGNMENT_BOUND_NANO = 5_000_000_000
LOSS_COUNTERS = ("rejected_spans", "span_conflicts", "parked_traces_discarded")


@dataclass(frozen=True)
class SentCall:
    """What the client side sent for one call."""

    session_key: str
    exported_span_count: int
    started_at_unix_nano: int
    # speaker -> the WAV bytes uploaded for that speaker's recording
    tracks: Mapping[str, bytes]


@dataclass(frozen=True)
class Earshot:
    """One running server, as a client and an operator see it.

    ``credential`` is a project API key (``None``: anonymous on loopback);
    ``catalog`` is the server's SQLite data directory or its ``postgresql://`` URL;
    ``read_blob`` reads one stored object by its blob key.
    """

    port: int
    project_id: str
    catalog: str | Path
    read_blob: Callable[[str], bytes]
    credential: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        sent = dict(headers or {})
        if self.credential is not None:
            sent["Authorization"] = f"Bearer {self.credential}"
        return request(self.port, method, path, body, headers=sent)

    def json(self, method: str, path: str, body: Any = None) -> Any:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = None if body is None else {"Content-Type": "application/json"}
        reply = self.call(method, path, encoded, headers)
        assert reply.status in {200, 201}, (method, path, reply.status, reply.body[:500])
        return reply.json()


@contextlib.contextmanager
def read_catalog(location: str | Path) -> Iterator[Any]:
    """One read snapshot of a catalog: a SQLite data directory or a ``postgresql://``
    URL. It takes no store lock, so a running server is not disturbed."""

    if isinstance(location, str) and location.startswith("postgresql"):
        from earshot.storage_postgres import PostgresDatabase

        database = PostgresDatabase(location, pool_size=2)
        try:
            connection = database.connect()
            try:
                connection.begin_read()
                yield connection
            finally:
                connection.rollback()
                connection.close()
        finally:
            database.close()
        return
    from earshot.storage_database import SQLiteDatabase

    sqlite = SQLiteDatabase(str(Path(location) / "earshot.sqlite3")).connect()
    try:
        sqlite.begin_read()
        yield sqlite
    finally:
        sqlite.rollback()
        sqlite.close()


def read_rows(location: str | Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple]:
    with read_catalog(location) as connection:
        return [tuple(row) for row in connection.execute(sql, parameters).fetchall()]


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def script_words(text: str) -> set[str]:
    """The words of four or more letters in ``text``, lower-cased."""

    return {word for word in re.findall(r"[a-z]+", text.lower()) if len(word) >= 4}


def wait_until_settled(
    earshot: Earshot, call_id: str, *, recordings: int, timeout: float = SETTLE_TIMEOUT_S
) -> dict[str, Any]:
    """The call's detail once it is closed, not dirty, holds no pending recording,
    and its latest revision holds ``recordings`` recordings."""

    deadline = time.monotonic() + timeout
    while True:
        detail = earshot.json("GET", f"/v1/calls/{call_id}")
        [(dirty,)] = read_rows(
            earshot.catalog,
            "SELECT dirty FROM calls WHERE project_id = ? AND call_id = ?",
            (earshot.project_id, call_id),
        )
        latest = detail["revisions"][-1] if detail["revisions"] else None
        seen = {
            "state": detail["state"],
            "dirty": bool(dirty),
            "recordings": sorted(item["state"] for item in detail["recordings"]),
            "latest_recording_count": None if latest is None else latest["recording_count"],
        }
        if (
            seen["state"] == "closed"
            and not seen["dirty"]
            and "pending" not in seen["recordings"]
            and seen["latest_recording_count"] == recordings
        ):
            return detail
        assert time.monotonic() < deadline, f"the call did not settle in {timeout} s: {seen}"
        time.sleep(0.5)


@dataclass(frozen=True)
class SettledCall:
    """A call that passed the checks: its API detail (for the caller's transcript
    checks), and how far its first ``stt`` span started from ``started_at``."""

    detail: dict[str, Any]
    first_stt_offset_nano: int


def assert_done_when(earshot: Earshot, sent: SentCall) -> SettledCall:
    """Wait for the call of ``sent`` to settle, then check FR-12's assertions."""

    call_id = earshot.json("POST", "/v1/calls:resolve", {"session_key": sent.session_key})[
        "call_id"
    ]
    detail = wait_until_settled(earshot, call_id, recordings=len(sent.tracks))
    project = (earshot.project_id,)

    # Exactly one call, in the catalog and in the API, for the key.
    assert read_rows(
        earshot.catalog,
        "SELECT call_id, session_key, state, dirty FROM calls WHERE project_id = ?",
        project,
    ) == [(call_id, sent.session_key, "closed", 0)]
    listed = earshot.json("GET", "/v1/calls")["items"]
    assert [(item["call_id"], item["session_key"]) for item in listed] == [
        (call_id, sent.session_key)
    ]

    # The latest revision closed on the end signal, and is final.
    latest = detail["revisions"][-1]
    assert detail["latest_revision"]["revision"] == latest["revision"]
    assert (latest["close_reason"], latest["finality"]) == ("end_signal", "final")
    assert detail["materialize_error"] is None
    assert any(
        line["text"].strip() for line in detail["transcript"] if line["speaker"] == "agent"
    ), detail["transcript"]

    # The stored recordings are the uploaded bytes, in the blob store and over HTTP.
    blob_keys = dict(
        read_rows(
            earshot.catalog,
            "SELECT recording_id, blob_key FROM recordings WHERE project_id = ? AND call_id = ?",
            (earshot.project_id, call_id),
        )
    )
    recordings = {item["speaker"]: item for item in detail["recordings"]}
    assert sorted(recordings) == sorted(sent.tracks)
    for speaker, uploaded in sent.tracks.items():
        recording, digest = recordings[speaker], hashlib.sha256(uploaded).hexdigest()
        assert (recording["state"], recording["sha256"], recording["size"]) == (
            "stored",
            digest,
            len(uploaded),
        ), speaker
        assert int(recording["started_at_unix_nano"]) == sent.started_at_unix_nano
        stored = earshot.read_blob(blob_keys[recording["recording_id"]])
        assert hashlib.sha256(stored).hexdigest() == digest, speaker
        content = earshot.call("GET", f"/v1/recordings/{recording['recording_id']}/content")
        assert (content.status, content.media_type) == (200, "audio/wav"), speaker
        assert hashlib.sha256(content.body).hexdigest() == digest, speaker
        assert wav_seconds(content.body) > 1.0, speaker

    # Every exported span is stored, and none was rejected, conflicted or discarded.
    [(stored_spans, first_stt)] = read_rows(
        earshot.catalog,
        "SELECT count(*), min(CASE WHEN scope_name = 'pipecat' AND name = 'stt' "
        "THEN start_unix_nano END) FROM spans WHERE project_id = ?",
        project,
    )
    assert (stored_spans, detail["span_count"], latest["span_count"]) == (
        sent.exported_span_count,
    ) * 3
    stats = earshot.json("GET", "/v1/otlp/stats")
    assert {name: stats[name] for name in LOSS_COUNTERS} == dict.fromkeys(LOSS_COUNTERS, 0)

    # The recordings and the spans share one timeline.
    assert first_stt is not None
    offset = int(first_stt) - sent.started_at_unix_nano
    assert abs(offset) < ALIGNMENT_BOUND_NANO
    return SettledCall(detail, offset)
