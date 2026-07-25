"""Gate: an in-flight continuous browser call survives a backend restart.

``captureVersion: 2`` accumulates a whole call on the backend, and the browser
keeps no copy of it -- so before this, a backend restart lost every in-flight
call and orphaned nothing recoverable. With a durable journal directory
configured, each call is written to disk (its frames and its drain-sequencing
ledger), and on the next process the live registry replays it:

* the rebuilt call is visible, tailable, sealable, and **provisional** -- a
  rebuild never fabricates a close;
* a client that continues the call resumes cleanly: its next in-sequence drain is
  accepted, a resent drain replays, a forged drain at a taken slot still
  conflicts, rather than the call being an ``unknown session``;
* the interval spanning the restart is declared ``carry_lost_on_restart`` coverage
  rather than estimated, because the raw snapshot the WebRTC delta needs was never
  in the journal;
* a call whose explicit ``endCall()`` was durably observed before the restart is
  still finalized after it, and a late drain is refused;
* without a journal directory nothing is written and a restart drops the call,
  exactly as it drops any other in-memory live session.

The last gate uses a real subprocess that is ``SIGKILL``ed mid-call, so the
resume is proven against genuine process death, not a cooperative teardown.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from earshot.analysis import analyze_incident
from earshot.api import ApiConfig, create_app
from earshot.contract import IncidentBundle
from earshot.storage import IncidentStore
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"

CLOCK_ID = "clk_0011223344556677"
HEADERS = {"Authorization": "Bearer t"}


def clock_domain() -> dict:
    return {
        "id": CLOCK_ID,
        "kind": "browser_monotonic",
        "unit": "ms",
        "uncertaintyMs": 1,
        "wallOriginMs": 1_700_000_000_000,
    }


def stats_snapshot(ts: float, received: int, lost: int = 0) -> dict:
    stats = {
        "IT": {
            "type": "inbound-rtp",
            "kind": "audio",
            "packetsReceived": received,
            "packetsLost": lost,
            "jitter": 0.01,
            "jitterBufferDelay": received / 1000.0,
            "jitterBufferEmittedCount": received,
            "concealedSamples": lost,
            "totalSamplesReceived": received * 10,
        }
    }
    return {"timestamp_ms": ts, "stats": stats}


def drain(
    sequence: int, snapshots: list[dict], *, session_id: str = "sess_durable", **extra
) -> dict:
    return {
        "captureVersion": 2,
        "sessionId": session_id,
        "clockDomain": clock_domain(),
        "drainSequence": sequence,
        "capturerStartedAtMs": 0,
        "snapshots": snapshots,
        **extra,
    }


def series(count: int) -> list[dict]:
    return [
        stats_snapshot(1000 + index * 100, 1000 + index * 100, 5 * index) for index in range(count)
    ]


def make_app(store: IncidentStore, capture_dir: Path) -> object:
    return create_app(
        store=store,
        config=ApiConfig(token="t"),
        analyzer=analyze_incident,
        capture_journal_dir=capture_dir,
    )


def seal(client: TestClient, call_id: str) -> dict:
    return client.post(f"/v1/live/sessions/{call_id}/seal", headers=HEADERS).json()


def fetch_bundle(client: TestClient, bundle_id: str) -> IncidentBundle:
    response = client.get(f"/v1/incidents/{bundle_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return IncidentBundle.model_validate(response.json())


def code(response) -> str:
    return response.json()["error"]["code"]


def continuity_reasons(bundle: IncidentBundle) -> set[str]:
    return {
        coverage.reason
        for coverage in bundle.profile.coverage
        if coverage.signal == "capture.stats_continuity"
    }


# -- the core restart resume ---------------------------------------------------


def test_a_call_survives_a_backend_restart_and_a_client_resumes(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    snaps = series(6)

    before = TestClient(make_app(store, capture_dir))
    call_id = None
    for index in range(4):
        response = before.post(
            "/v1/capture", json=drain(index + 1, [snaps[index]]), headers=HEADERS
        )
        assert response.status_code == 202, response.text
        call_id = response.json()["call_id"]

    # A restart: drop every in-memory registry. The durable journal stays on disk.
    before.app.state.live.close()

    after = TestClient(make_app(store, capture_dir))

    # The rebuilt call is visible on the live surface, not an unknown session.
    listed = after.get("/v1/live/sessions", headers=HEADERS).json()["items"]
    assert any(item["session_id"] == call_id for item in listed)

    # The next in-sequence drain is accepted onto the same call.
    resumed = after.post("/v1/capture", json=drain(5, [snaps[4]]), headers=HEADERS)
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["call_id"] == call_id

    # A resent drain the call already applied before the restart replays.
    replay = after.post("/v1/capture", json=drain(3, [snaps[2]]), headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True

    # A forged drain at a taken slot still conflicts.
    forged = after.post(
        "/v1/capture", json=drain(2, [stats_snapshot(9999, 9999, 999)]), headers=HEADERS
    )
    assert forged.status_code == 409
    assert code(forged) == "EARSHOT_CAPTURE_SEQUENCE_CONFLICT"

    # And a gap with no declared loss is still refused, naming the expected seq.
    gap = after.post("/v1/capture", json=drain(9, [snaps[5]]), headers=HEADERS)
    assert gap.status_code == 409
    assert code(gap) == "EARSHOT_CAPTURE_SEQUENCE_GAP"


def test_a_rebuilt_call_seals_provisional_and_never_fabricates_a_close(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    snaps = series(5)

    before = TestClient(make_app(store, capture_dir))
    call_id = None
    for index in range(5):
        call_id = before.post(
            "/v1/capture", json=drain(index + 1, [snaps[index]]), headers=HEADERS
        ).json()["call_id"]
    before.app.state.live.close()

    after = TestClient(make_app(store, capture_dir))
    sealed = seal(after, call_id)
    assert sealed["finality"] == "provisional"
    assert sealed["close_observed"] is False

    bundle = fetch_bundle(after, sealed["bundle_id"])
    manifest = bundle.profile.manifest
    assert manifest.finality == "provisional"
    assert bundle.profile.session.ended_at is None
    assert manifest.recovery is not None
    assert manifest.recovery.method == "browser_capture_journal"
    assert manifest.recovery.close_observed is False
    assert validate_incident(bundle).ok


def test_a_call_continued_after_a_restart_seals_into_a_valid_artifact(tmp_path) -> None:
    """Continuing across a restart yields one valid artifact that declares the gap."""

    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    snaps = series(8)

    before = TestClient(make_app(store, capture_dir))
    call_id = None
    for index in range(4):
        call_id = before.post(
            "/v1/capture", json=drain(index + 1, [snaps[index]]), headers=HEADERS
        ).json()["call_id"]
    before.app.state.live.close()

    after = TestClient(make_app(store, capture_dir))
    for index in range(4, 8):
        response = after.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=HEADERS)
        assert response.status_code == 202, response.text

    sealed = seal(after, call_id)
    bundle = fetch_bundle(after, sealed["bundle_id"])
    # The artifact is whole and valid -- pre- and post-restart facts carry no
    # colliding ids and no corrupted supersession.
    assert validate_incident(bundle).ok
    assert sealed["finality"] == "provisional"
    # The interval spanning the restart is declared lost, not estimated.
    assert "carry_lost_on_restart" in continuity_reasons(bundle)


def test_a_durably_ended_call_stays_final_and_refuses_a_late_drain(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    snaps = series(3)

    before = TestClient(make_app(store, capture_dir))
    call_id = None
    for index in range(2):
        call_id = before.post(
            "/v1/capture", json=drain(index + 1, [snaps[index]]), headers=HEADERS
        ).json()["call_id"]
    ended = before.post(
        "/v1/capture",
        json=drain(3, [snaps[2]], end={"reason": "call_ended", "timestampMs": 1400}),
        headers=HEADERS,
    )
    assert ended.status_code == 202
    assert ended.json()["finalized"] is True
    before.app.state.live.close()

    after = TestClient(make_app(store, capture_dir))
    # A brand-new drain after a durably-observed close is refused, not appended.
    late = after.post("/v1/capture", json=drain(4, [stats_snapshot(1500, 4000)]), headers=HEADERS)
    assert late.status_code == 409
    assert code(late) == "EARSHOT_CAPTURE_CALL_CLOSED"

    # And the rebuilt call still seals into a final artifact.
    sealed = seal(after, call_id)
    assert sealed["finality"] == "final"
    assert sealed["close_observed"] is True
    bundle = fetch_bundle(after, sealed["bundle_id"])
    assert bundle.profile.session.ended_at is not None
    assert validate_incident(bundle).ok


def test_without_a_journal_directory_a_restart_drops_the_call(tmp_path) -> None:
    """Durability is opt-in: no directory, no on-disk copy, and a restart loses it."""

    store = IncidentStore(tmp_path / "store")
    before = create_app(store=store, config=ApiConfig(token="t"), analyzer=analyze_incident)
    client = TestClient(before)
    call_id = None
    for index in range(3):
        call_id = client.post(
            "/v1/capture", json=drain(index + 1, [series(3)[index]]), headers=HEADERS
        ).json()["call_id"]
    before.state.live.close()

    after = create_app(
        store=IncidentStore(tmp_path / "store"),
        config=ApiConfig(token="t"),
        analyzer=analyze_incident,
    )
    after_client = TestClient(after)
    listed = after_client.get("/v1/live/sessions", headers=HEADERS).json()["items"]
    assert all(item["session_id"] != call_id for item in listed)


# -- a real crash --------------------------------------------------------------


def _crash_script(capture_dir: Path, drains: int) -> str:
    return textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(SDK_SRC)!r})
        from earshot.live import LiveSessionRegistry
        from earshot.capture.calls import CaptureCallRegistry, CaptureDrain

        def make(seq, received):
            snap = {{
                "timestamp_ms": 1000 + seq * 100,
                "stats": {{"IT": {{
                    "type": "inbound-rtp", "kind": "audio",
                    "packetsReceived": received, "packetsLost": 0,
                    "jitterBufferDelay": received / 1000.0,
                    "jitterBufferEmittedCount": received,
                }}}},
            }}
            return CaptureDrain(
                project_id="default", session_id="sess_crash", capture_version=2,
                clock_domain_id={CLOCK_ID!r}, clock_uncertainty_ms=1.0,
                clock_wall_origin_ms=1700000000000.0, trace_id=None, span_id=None,
                drain_sequence=seq, snapshots=[snap], device_events=[],
                coverage=(), rejection_coverage=(), resync=None, end=None,
            )

        live = LiveSessionRegistry()
        registry = CaptureCallRegistry(live, journal_dir={str(capture_dir)!r})
        call_id = None
        for seq in range(1, {drains} + 1):
            call_id = registry.drain(make(seq, 1000 + seq * 100)).call_id
        sys.stdout.write(call_id)
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )


def test_a_sigkilled_backend_leaves_a_resumable_call_on_disk(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    completed = subprocess.run(
        [sys.executable, "-c", _crash_script(capture_dir, 5)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == -9, completed.stderr.decode()
    call_id = completed.stdout.decode().strip()
    assert call_id

    # Rebuild the whole live+capture state from what the killed process left.
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.live import LiveSessionRegistry

    def make(seq: int, received: int) -> CaptureDrain:
        snap = {
            "timestamp_ms": 1000 + seq * 100,
            "stats": {
                "IT": {
                    "type": "inbound-rtp",
                    "kind": "audio",
                    "packetsReceived": received,
                    "packetsLost": 0,
                    "jitterBufferDelay": received / 1000.0,
                    "jitterBufferEmittedCount": received,
                }
            },
        }
        return CaptureDrain(
            project_id="default",
            session_id="sess_crash",
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=seq,
            snapshots=[snap],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
            end=None,
        )

    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    registry.rebuild_from_disk()
    assert live.contains(call_id, project_id="default")

    # In-sequence drain accepted onto the rebuilt call.
    resumed = registry.drain(make(6, 1600))
    assert resumed.replay is False
    assert resumed.call_id == call_id

    # A resent, already-applied drain replays without re-journaling.
    replayed = registry.drain(make(5, 1500))
    assert replayed.replay is True

    # A forged drain at a taken slot conflicts.
    with pytest.raises(CaptureSequenceConflictError):
        registry.drain(make(4, 424242))

    # The rebuilt call is still provisional: no close was ever observed.
    summary = live.summary(call_id, project_id="default")
    assert summary.close_observed is False
    live.close()
