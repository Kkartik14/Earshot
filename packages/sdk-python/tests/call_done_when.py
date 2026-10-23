"""The done-when checks for one call, shared by the end-to-end tests.

The tests drive a real ``earshot serve`` process with one call's evidence: a real
Pipecat or LiveKit call (``apps/ingest/tests/e2e_real``) or a real capture replayed
(``apps/ingest/tests/e2e``). They then ask the same questions of the server's
HTTP API, its catalog and its blob store.

``assert_done_when`` (a call with WAV recordings):

- the call settles: ``state=closed``, not ``dirty``, no pending recording, and the
  latest revision holds both recordings (hard timeout 120 s);
- exactly one call exists for the session key, and its latest revision closed on
  the end signal and is final;
- the agent said something; the stored recordings are the uploaded bytes, and
  their content is a WAV longer than 1 s;
- every exported span is stored, and no span was rejected, conflicted or parked
  and discarded;
- the recordings' ``started_at`` is within 5 s of the first ``stt`` span.

``wait_until_projected`` then ``assert_call_view`` (the call view):

- the projection is current for the latest revision, and every recording's
  analysis is stored;
- every turn's audible gap at the agent is measured, with a value in (0, 15 s) and
  a ± of at most 100 ms (the release's accuracy target), or not measurable with
  its reason, or a talk-over; at least one turn is measured; the framework's own
  latency is reported for at least one turn;
- the cost has a lower bound above 0, and each provider of the call (STT, LLM, TTS)
  has a priced or estimated item; the totals are the sums of their items;
- the transcript is placed on the call's axis: every line has measured times
  inside the recording, and each measured turn's agent speech starts a line.

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

# The call view (FR-11).
PROJECTION_TIMEOUT_S = 120.0
GAP_RANGE_MS = (0, 15_000)  # exclusive: a real turn's audible gap lies inside it
MAX_GAP_UNCERTAINTY_MS = 100  # the release's accuracy target for a measured gap
PLACEMENT_TOLERANCE_MS = 300
NOT_MEASURABLE_REASONS = frozenset(
    {
        "recorder_timeline_discontinuity",
        "no_agent_audio",
        "no_clear_pairing",
        "not_one_timeline",
        "no_recording",
        "analysis_not_stored",
        "boundary_uncertainty_too_high",
    }
)
GAP_BASES = frozenset({"anchors_checked", "continuous_timeline_assumed"})
# The operation -> the meters that make its cost, and the statuses that count.
COSTED = {
    "llm": ({"input_token", "output_token"}, {"priced"}),
    "tts": ({"character"}, {"priced"}),
    "stt": ({"audio_second"}, {"priced", "estimated"}),
}


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

    def send(
        self, method: str, path: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        """One request as ``capture_replay`` sends it: its status and body."""

        reply = self.call(method, path, body, headers)
        return reply.status, reply.body


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


def wait_until_projected(
    earshot: Earshot, call_id: str, *, timeout: float = PROJECTION_TIMEOUT_S
) -> dict[str, Any]:
    """The call's detail once its projection is current for its latest revision and
    no analysis of its recordings is still pending or running.

    The server projects a call after each revision and again after each analysis
    result, so a current projection can say ``pending`` until the analyses end."""

    deadline = time.monotonic() + timeout
    while True:
        detail = earshot.json("GET", f"/v1/calls/{call_id}")
        projection, latest = detail["projection"], detail["latest_revision"]
        seen = {
            "projection_state": detail["projection_state"],
            "revision": None if projection is None else projection["revision"],
            "latest_revision": None if latest is None else latest["revision"],
            "analysis_state": None if projection is None else projection["analysis_state"],
        }
        if (
            seen["projection_state"] == "current"
            and seen["revision"] == seen["latest_revision"]
            and seen["analysis_state"] != "pending"
        ):
            return detail
        assert time.monotonic() < deadline, f"the call was not projected in {timeout} s: {seen}"
        time.sleep(0.5)


def _assert_turns(turns: list[dict[str, Any]], measurable_turns: int) -> list[dict[str, Any]]:
    """Every turn's audible gap is measured, not measurable with a reason, or a
    talk-over; at least one is measured. The measured turns."""

    low, high = GAP_RANGE_MS
    measured = []
    for turn in turns:
        gap = turn["audible_gap"]
        if gap["status"] == "measured":
            assert gap["reason"] is None and gap["basis"] in GAP_BASES, turn
            assert low < gap["value_ms"] < high, turn
            assert gap["uncertainty_ms"] is not None, turn
            assert gap["uncertainty_ms"] <= MAX_GAP_UNCERTAINTY_MS, turn
            assert gap["agent_start_ms"] - gap["user_end_ms"] == gap["value_ms"], turn
            measured.append(turn)
        elif gap["status"] == "not_measurable":
            assert gap["reason"] in NOT_MEASURABLE_REASONS and gap["value_ms"] is None, turn
            # Only a discontinuity bounds the gap ("≤ raw gap"); nothing else guesses.
            assert gap["bound_ms"] is None or gap["reason"] == "recorder_timeline_discontinuity"
        else:
            assert (gap["status"], gap["value_ms"]) == ("talk_over", None), turn
        assert turn["reported_latency_ms"] is None or turn["reported_latency_ms"] > 0, turn
    assert measured, [turn["audible_gap"] for turn in turns]
    assert measurable_turns == len(measured)
    assert any(turn["reported_latency_ms"] is not None for turn in turns), turns
    return measured


def _assert_cost(projection: dict[str, Any], providers: Mapping[str, Mapping[str, str]]) -> None:
    """A lower bound above 0, the totals the sums of their items, and a costed item
    from each of the call's providers."""

    cost, items = projection["cost"], projection["cost_items"]
    priced = [item["cost_nano_usd"] for item in items if item["status"] == "priced"]
    estimated = [item["cost_nano_usd"] for item in items if item["status"] == "estimated"]
    assert cost["lower_bound_nano_usd"] is not None and cost["lower_bound_nano_usd"] > 0, cost
    assert cost["lower_bound_nano_usd"] == sum(priced), (cost, items)
    assert cost["estimated_nano_usd"] == (sum(estimated) if estimated else None), (cost, items)
    unknown = [item for item in items if item["status"] in ("no_price", "not_reported")]
    assert cost["unknown_items"] == len(unknown), (cost, items)
    operations = {operation for operation in providers if operation in COSTED}
    assert {item["operation"] for item in items} == operations, items
    for operation in operations:
        units, statuses = COSTED[operation]
        own = [item for item in items if item["operation"] == operation]
        assert {item["provider"] for item in own} == {providers[operation]["provider"]}, own
        costed = {
            item["unit"]
            for item in own
            if item["status"] in statuses and (item["cost_nano_usd"] or 0) > 0
        }
        assert units <= costed, (operation, own)


def _assert_placed(projection: dict[str, Any], measured: list[dict[str, Any]]) -> None:
    """Every line has measured times inside the recordings, and each measured turn's
    agent speech starts an agent line."""

    transcript, media = projection["transcript"], projection["media"]
    assert {line["speaker"] for line in transcript} == {"user", "agent"}, transcript
    first = min(item["offset_ms"] for item in media) - PLACEMENT_TOLERANCE_MS
    last = max(item["offset_ms"] + item["duration_ms"] for item in media) + PLACEMENT_TOLERANCE_MS
    for line in transcript:
        assert line["timing"] == "measured", line
        assert first <= line["start_ms"] <= last and line["start_ms"] <= line["end_ms"], line
    for turn in measured:
        onset = turn["audible_gap"]["agent_start_ms"]
        assert any(
            line["speaker"] == "agent" and abs(line["start_ms"] - onset) <= PLACEMENT_TOLERANCE_MS
            for line in transcript
        ), (turn, transcript)


def assert_call_view(
    detail: dict[str, Any], providers: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """Check a projected call's view (the module docstring's second list).

    ``providers`` maps each operation the call ran (``stt``, ``llm``, ``tts``; other
    keys are ignored) to ``{"provider": …}``. Returns the view's numbers, for a
    test's summary."""

    projection = detail["projection"]
    assert detail["projection_state"] == "current" and projection is not None, detail
    assert projection["revision"] == detail["latest_revision"]["revision"], projection
    assert projection["analysis_state"] == "stored", projection["media"]
    assert projection["media"], "the call view has no playable media"
    for media in projection["media"]:
        assert (media["analysis_state"], media["analysis_error_code"]) == ("stored", None), media
        assert media["peaks_path"] is not None and media["duration_ms"] is not None, media
    speakers = {channel["speaker"] for item in projection["media"] for channel in item["channels"]}
    assert speakers == {"user", "agent"}, projection["media"]
    turns = projection["turns"]
    assert turns and projection["turn_count"] == len(turns), projection["turn_count"]
    measured = _assert_turns(turns, projection["measurable_turns"])
    _assert_cost(projection, providers)
    _assert_placed(projection, measured)
    return {
        "analysis_state": projection["analysis_state"],
        "media": [
            {key: item[key] for key in ("source", "content_type", "duration_ms", "channels")}
            for item in projection["media"]
        ],
        "turns": [
            {"reported_latency_ms": turn["reported_latency_ms"]}
            | {
                key: turn["audible_gap"][key]
                for key in ("status", "value_ms", "uncertainty_ms", "bound_ms", "reason", "basis")
            }
            for turn in turns
        ],
        "cost": projection["cost"],
        "cost_items": [
            [item[key] for key in ("status", "operation", "provider", "model", "unit", "quantity")]
            for item in projection["cost_items"]
        ],
        "transcript_lines": len(projection["transcript"]),
    }
