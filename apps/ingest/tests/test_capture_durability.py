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
import threading
from concurrent.futures import ThreadPoolExecutor
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


def make_app(
    store: IncidentStore,
    capture_dir: Path,
    *,
    max_sealed_capture_replay_ledgers: int = 1024,
) -> object:
    if not capture_dir.exists():
        capture_dir.mkdir(parents=True)
    return create_app(
        store=store,
        config=ApiConfig(
            token="t",
            max_sealed_capture_replay_ledgers=max_sealed_capture_replay_ledgers,
        ),
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


def test_capture_journal_directory_has_one_live_writer(tmp_path) -> None:
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureJournalWriterConflictError,
    )
    from earshot.live import LiveSessionRegistry

    directory = tmp_path / "capture"
    directory.mkdir()
    first = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    try:
        with pytest.raises(CaptureJournalWriterConflictError):
            CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    finally:
        first.close()

    restarted = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    restarted.close()


def test_capture_journal_writer_lease_retries_after_directory_repair(tmp_path) -> None:
    from earshot.capture import CaptureCallRegistry, CaptureJournalWriterConflictError
    from earshot.live import LiveSessionRegistry

    directory = tmp_path / "capture"
    directory.write_bytes(b"not a directory")
    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    try:
        assert registry.drop_project("tenant-a") is False
        directory.unlink()
        directory.mkdir()
        assert registry.drop_project("tenant-a") is True
        with pytest.raises(CaptureJournalWriterConflictError):
            CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    finally:
        registry.close()


def test_missing_capture_journal_directory_keeps_project_deletion_pending(tmp_path) -> None:
    from earshot.capture import CaptureCallRegistry
    from earshot.live import LiveSessionRegistry

    directory = tmp_path / "capture"
    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    try:
        assert not directory.exists()
        assert registry.drop_project("tenant-a") is False
        assert not directory.exists()
    finally:
        registry.close()


def test_app_lifespan_releases_capture_journal_writer_lease(tmp_path) -> None:
    from earshot.capture import CaptureCallRegistry, CaptureJournalWriterConflictError
    from earshot.live import LiveSessionRegistry

    directory = tmp_path / "capture"
    app = make_app(IncidentStore(tmp_path / "store"), directory)
    with TestClient(app), pytest.raises(CaptureJournalWriterConflictError):
        CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)

    restarted = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=directory)
    restarted.close()


def test_capture_journal_cannot_share_a_directory_with_checkpoint_sources(tmp_path) -> None:
    from earshot.live import LiveSessionRegistry

    directory = tmp_path / "journals"
    with pytest.raises(ValueError, match="must differ from the checkpoint directory"):
        create_app(
            store=IncidentStore(tmp_path / "store"),
            live_registry=LiveSessionRegistry(journal_dir=directory),
            capture_journal_dir=directory,
        )


def test_unavailable_capture_directory_returns_retryable_error(tmp_path) -> None:
    capture_path = tmp_path / "capture-files"
    capture_path.write_bytes(b"capture directory is unavailable")
    app = make_app(IncidentStore(tmp_path / "store"), capture_path)

    with TestClient(app) as client:
        response = client.post(
            "/v1/capture",
            json=drain(1, [series(1)[0]]),
            headers=HEADERS,
        )
        capture_path.unlink()
        capture_path.mkdir()
        retried = client.post(
            "/v1/capture",
            json=drain(1, [series(1)[0]]),
            headers=HEADERS,
        )

    assert response.status_code == 503
    assert code(response) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert retried.status_code == 202, retried.text


def test_corrupt_capture_sidecar_cannot_replace_or_append_to_its_journal(tmp_path) -> None:
    from earshot.capture.durable import journal_path, sidecar_path

    capture_dir = tmp_path / "capture"
    store = IncidentStore(tmp_path / "store")
    first_app = make_app(store, capture_dir)
    body = drain(1, [series(1)[0]])
    with TestClient(first_app) as client:
        accepted = client.post("/v1/capture", json=body, headers=HEADERS)
    assert accepted.status_code == 202, accepted.text

    call_id = accepted.json()["call_id"]
    journal = journal_path(capture_dir, call_id)
    ledger = sidecar_path(capture_dir, call_id)
    journal_before = journal.read_bytes()
    corrupt_ledger = b"{broken ledger"
    ledger.write_bytes(corrupt_ledger)

    second_app = make_app(store, capture_dir)
    with TestClient(second_app) as client:
        rejected = client.post("/v1/capture", json=body, headers=HEADERS)

    assert rejected.status_code == 503
    assert code(rejected) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert journal.read_bytes() == journal_before
    assert ledger.read_bytes() == corrupt_ledger


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sealed", "false"),
        ("applied", True),
        ("applied", "1"),
        ("digests", []),
        ("digests", {"1": "z" * 64}),
        ("digests", {"01": "0" * 64}),
        ("first_observed_ms", float("nan")),
        ("sealed", True),
    ],
)
def test_parseable_invalid_capture_sidecar_never_authorizes_journal_deletion(
    tmp_path, field: str, value: object
) -> None:
    import json

    from earshot.capture.durable import journal_path, sidecar_path

    capture_dir = tmp_path / "capture"
    store = IncidentStore(tmp_path / "store")
    body = drain(1, [series(1)[0]])
    with TestClient(make_app(store, capture_dir)) as client:
        accepted = client.post("/v1/capture", json=body, headers=HEADERS)
    assert accepted.status_code == 202, accepted.text

    call_id = accepted.json()["call_id"]
    journal = journal_path(capture_dir, call_id)
    ledger = sidecar_path(capture_dir, call_id)
    journal_before = journal.read_bytes()
    document = json.loads(ledger.read_text())
    document[field] = value
    invalid_ledger = json.dumps(document, allow_nan=True).encode()
    ledger.write_bytes(invalid_ledger)

    with TestClient(make_app(store, capture_dir), raise_server_exceptions=False) as client:
        rejected = client.post("/v1/capture", json=body, headers=HEADERS)

    assert rejected.status_code == 503
    assert code(rejected) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert journal.read_bytes() == journal_before
    assert ledger.read_bytes() == invalid_ledger


# -- the core restart resume ---------------------------------------------------


def test_failed_sidecar_persistence_does_not_create_an_unowned_journal(
    tmp_path, monkeypatch
) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    client = TestClient(make_app(IncidentStore(tmp_path / "store"), capture_dir))

    def fail_replace(_source, _destination) -> None:
        raise OSError("simulated disk write failure")

    monkeypatch.setattr("earshot.capture.durable.os.replace", fail_replace)
    response = client.post(
        "/v1/capture",
        json=drain(1, [series(1)[0]]),
        headers=HEADERS,
    )

    assert response.status_code == 503
    assert code(response) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert not tuple(capture_dir.glob("*.eck"))


def test_interrupted_drain_before_append_is_retried_after_restart(tmp_path, monkeypatch) -> None:
    """A durable intent without frames must not be reported as an applied replay."""

    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    before = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    first = before.post("/v1/capture", json=drain(1, [series(2)[0]]), headers=HEADERS)
    assert first.status_code == 202, first.text
    prior_sequence = first.json()["accepted_through"]

    def crash_before_append(*_args, **_kwargs):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(before.app.state.live, "accept_records", crash_before_append)
    interrupted = before.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)
    assert interrupted.status_code == 500

    before.app.state.live.close()
    before.app.state.capture_calls.close()
    after = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    forged = after.post(
        "/v1/capture",
        json=drain(2, [stats_snapshot(9999, 9999, 999)]),
        headers=HEADERS,
    )
    assert forged.status_code == 409
    assert code(forged) == "EARSHOT_CAPTURE_SEQUENCE_CONFLICT"
    retried = after.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)

    assert retried.status_code == 202, retried.text
    assert retried.json()["replayed"] is False
    assert retried.json()["accepted_through"] > prior_sequence


def test_interrupted_drain_after_append_is_applied_once_after_restart(
    tmp_path, monkeypatch
) -> None:
    """A durable but uncommitted append is recovered without duplicating its frames."""

    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    before = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    first = before.post("/v1/capture", json=drain(1, [series(2)[0]]), headers=HEADERS)
    assert first.status_code == 202, first.text
    prior_sequence = first.json()["accepted_through"]

    original_accept_records = before.app.state.live.accept_records

    def append_then_interrupt(*args, **kwargs):
        original_accept_records(*args, **kwargs)
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(before.app.state.live, "accept_records", append_then_interrupt)
    interrupted = before.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)
    assert interrupted.status_code == 500

    before.app.state.live.close()
    before.app.state.capture_calls.close()
    after = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    retried = after.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)

    assert retried.status_code in {200, 202}, retried.text
    assert retried.json()["replayed"] is True
    assert retried.json()["accepted_records"] == 0
    assert retried.json()["accepted_through"] > prior_sequence


def test_failed_commit_marker_is_retried_before_replay_or_next_restart(
    tmp_path, monkeypatch
) -> None:
    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    client = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    from earshot.capture import durable as capture_durable

    original_replace = capture_durable.os.replace
    replacements = 0

    def fail_commit_rename(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated commit-sidecar rename failure")
        original_replace(source, destination)

    monkeypatch.setattr("earshot.capture.durable.os.replace", fail_commit_rename)
    body = drain(1, [series(1)[0]])
    interrupted = client.post("/v1/capture", json=body, headers=HEADERS)
    assert interrupted.status_code == 503
    assert interrupted.headers["Retry-After"] == "3"
    assert client.get("/v1/live/sessions", headers=HEADERS).json()["items"] == []

    same_process_retry = client.post("/v1/capture", json=body, headers=HEADERS)
    assert same_process_retry.status_code == 202, same_process_retry.text
    assert same_process_retry.json()["replayed"] is False
    accepted_through = same_process_retry.json()["accepted_through"]
    client.app.state.live.close()
    client.app.state.capture_calls.close()

    after_restart = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    restarted_retry = after_restart.post("/v1/capture", json=body, headers=HEADERS)
    assert restarted_retry.status_code == 200, restarted_retry.text
    assert restarted_retry.json()["replayed"] is True
    assert restarted_retry.json()["accepted_through"] == accepted_through


def test_failed_drain_recovery_serializes_an_exact_retry(tmp_path, monkeypatch) -> None:
    from earshot.capture import calls as capture_calls
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureJournalUnavailableError,
    )
    from earshot.live import LiveSessionRegistry

    def make(sequence: int, received: int) -> CaptureDrain:
        return CaptureDrain(
            project_id="default",
            session_id="sess_recovery_race",
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=sequence,
            snapshots=[stats_snapshot(1000 + sequence * 100, received)],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    first = registry.drain(make(1, 1100))

    original_write_sidecar = capture_calls.write_sidecar
    failed_commit = False

    def fail_commit_once(directory, sidecar) -> None:
        nonlocal failed_commit
        if sidecar.applied == 2 and sidecar.pending_sequence is None and not failed_commit:
            failed_commit = True
            raise OSError("simulated commit-marker failure")
        original_write_sidecar(directory, sidecar)

    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    original_recover = capture_calls.recover_pending_sidecar

    def pause_recovery(directory, sidecar):
        recovery_entered.set()
        if not release_recovery.wait(timeout=3):
            raise TimeoutError("test did not release capture recovery")
        return original_recover(directory, sidecar)

    monkeypatch.setattr(capture_calls, "write_sidecar", fail_commit_once)
    monkeypatch.setattr(capture_calls, "recover_pending_sidecar", pause_recovery)
    retry_started = threading.Event()
    retry_finished = threading.Event()

    def exact_retry():
        retry_started.set()
        try:
            return registry.drain(make(2, 1200))
        finally:
            retry_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        interrupted = pool.submit(registry.drain, make(2, 1200))
        assert recovery_entered.wait(timeout=2)
        retry = pool.submit(exact_retry)
        assert retry_started.wait(timeout=2)
        retry_was_blocked_by_recovery = not retry_finished.wait(timeout=0.1)
        release_recovery.set()

        with pytest.raises(CaptureJournalUnavailableError):
            interrupted.result(timeout=3)
        resumed = retry.result(timeout=3)

    assert retry_was_blocked_by_recovery
    assert resumed.replay is False
    assert resumed.accepted_records > 0
    summary = live.summary(first.call_id, project_id="default")
    assert summary.last_sequence == first.accepted_through + resumed.accepted_records
    live.close()


def test_unreplayable_journal_cannot_be_replaced_by_a_fresh_capture_call(tmp_path) -> None:
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureJournalUnavailableError,
    )
    from earshot.capture.durable import (
        CaptureSidecar,
        journal_path,
        sidecar_path,
        write_sidecar,
    )
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    drain_request = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_unreplayable_journal",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1200)],
        device_events=[],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )
    key_id = "a" * 64
    write_sidecar(
        capture_dir,
        CaptureSidecar(
            project_id=drain_request.project_id,
            call_key=drain_request.call_key,
            applied=1,
            digests={1: drain_request.digest()},
            turn_sequence=0,
            first_observed_ms=None,
            last_observed_ms=None,
            lossy=False,
            finalized=False,
            correlation_key_id=key_id,
        ),
    )
    journal = journal_path(capture_dir, drain_request.call_key)
    corrupt_bytes = b"not a replayable checkpoint journal"
    journal.write_bytes(corrupt_bytes)
    sidecar_before = sidecar_path(capture_dir, drain_request.call_key).read_bytes()
    registry = CaptureCallRegistry(
        LiveSessionRegistry(),
        journal_dir=capture_dir,
        correlation_key_id=key_id,
    )

    with pytest.raises(CaptureJournalUnavailableError, match="cannot be replayed"):
        registry.drain(drain_request)

    assert journal.read_bytes() == corrupt_bytes
    assert sidecar_path(capture_dir, drain_request.call_key).read_bytes() == sidecar_before
    assert registry.durable_available is False


def test_a_capture_drain_cannot_grow_its_durable_journal_past_the_byte_limit(tmp_path) -> None:
    from earshot.live import LiveConfig, LiveSessionRegistry

    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    live = LiveSessionRegistry(config=LiveConfig(max_capture_journal_bytes=32 * 1024))
    app = create_app(
        store=store,
        config=ApiConfig(token="t"),
        analyzer=analyze_incident,
        live_registry=live,
        capture_journal_dir=capture_dir,
    )

    with TestClient(app) as client:
        first = client.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
        assert first.status_code == 202, first.text
        journal = next(capture_dir.glob("*.eck"))
        committed_prefix = journal.read_bytes()

        oversized = drain(
            2,
            [stats_snapshot(1100 + index * 100, 1100 + index * 100, index) for index in range(100)],
        )
        rejected = client.post("/v1/capture", json=oversized, headers=HEADERS)

        assert rejected.status_code == 413, rejected.text
        assert code(rejected) == "EARSHOT_CAPTURE_JOURNAL_LIMIT"
        assert journal.read_bytes() == committed_prefix
        listed = client.get("/v1/live/sessions", headers=HEADERS).json()["items"]
        committed_session = next(
            item for item in listed if item["session_id"] == first.json()["call_id"]
        )
        assert committed_session["last_sequence"] == first.json()["accepted_through"]

        retried = client.post("/v1/capture", json=oversized, headers=HEADERS)
        assert retried.status_code == 413, retried.text
        assert code(retried) == "EARSHOT_CAPTURE_JOURNAL_LIMIT"
        assert journal.read_bytes() == committed_prefix
        assert any(
            item["session_id"] == first.json()["call_id"]
            for item in client.get("/v1/live/sessions", headers=HEADERS).json()["items"]
        )

        replacement = client.post(
            "/v1/capture",
            json=drain(2, [series(2)[1]]),
            headers=HEADERS,
        )
        assert replacement.status_code == 202, replacement.text
        assert replacement.json()["accepted_through"] > first.json()["accepted_through"]

        stale_retry = client.post("/v1/capture", json=oversized, headers=HEADERS)
        assert stale_retry.status_code == 409, stale_retry.text
        assert code(stale_retry) == "EARSHOT_CAPTURE_SEQUENCE_CONFLICT"

        oversized_first = drain(
            1,
            [stats_snapshot(1000 + index * 100, 1000 + index * 100, index) for index in range(100)],
            session_id="sess_oversized_first_drain",
        )
        rejected_first = client.post("/v1/capture", json=oversized_first, headers=HEADERS)
        assert rejected_first.status_code == 413, rejected_first.text
        assert code(rejected_first) == "EARSHOT_CAPTURE_JOURNAL_LIMIT"
        assert len(list(capture_dir.glob("*.eck"))) == 1

        retried_first = client.post("/v1/capture", json=oversized_first, headers=HEADERS)
        assert retried_first.status_code == 413, retried_first.text
        assert code(retried_first) == "EARSHOT_CAPTURE_JOURNAL_LIMIT"
        assert len(list(capture_dir.glob("*.eck"))) == 1


def test_capture_journal_limit_allows_a_smaller_same_sequence_retry_after_restart(tmp_path) -> None:
    from dataclasses import replace

    from earshot.capture.calls import CaptureCallRegistry, CaptureDrain
    from earshot.live import LiveCaptureJournalLimitError, LiveConfig, LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()

    def make_live() -> LiveSessionRegistry:
        return LiveSessionRegistry(config=LiveConfig(max_capture_journal_bytes=32 * 1024))

    first = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_replace_after_limit",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )
    oversized = replace(
        first,
        drain_sequence=2,
        snapshots=[stats_snapshot(1200 + index * 100, 1200 + index) for index in range(100)],
    )
    smaller = replace(oversized, snapshots=[stats_snapshot(1200, 1201)])

    live = make_live()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    accepted = registry.drain(first)
    with pytest.raises(LiveCaptureJournalLimitError):
        registry.drain(oversized)
    live.close()
    registry.close()

    restarted_live = make_live()
    restarted = CaptureCallRegistry(restarted_live, journal_dir=capture_dir)
    restarted.rebuild_from_disk()

    resumed = restarted.drain(smaller)
    assert resumed.replay is False
    assert resumed.call_id == accepted.call_id
    assert restarted_live.contains(resumed.call_id, project_id="tenant-a")

    restarted_live.close()
    restarted.close()


def test_first_drain_journal_limit_does_not_occupy_project_capacity(tmp_path) -> None:
    from dataclasses import replace

    from earshot.capture.calls import CaptureCallRegistry, CaptureDrain
    from earshot.capture.durable import sidecar_path
    from earshot.live import LiveCaptureJournalLimitError, LiveConfig, LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    live = LiveSessionRegistry(config=LiveConfig(max_capture_journal_bytes=32 * 1024))
    registry = CaptureCallRegistry(live, journal_dir=capture_dir, max_calls_per_project=1)
    oversized = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_rejected_first_drain",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1200 + index * 100, 1200 + index) for index in range(100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )

    with pytest.raises(LiveCaptureJournalLimitError):
        registry.drain(oversized)
    assert not sidecar_path(capture_dir, oversized.call_key).exists()

    replacement = replace(
        oversized,
        session_id="sess_after_rejected_first_drain",
        snapshots=[stats_snapshot(1200, 1201)],
    )
    accepted = registry.drain(replacement)
    assert accepted.replay is False
    assert live.contains(replacement.call_key, project_id="tenant-a")

    live.close()
    registry.close()


def test_dormant_durable_capture_count_is_bounded_across_projects(tmp_path) -> None:
    from earshot.capture.calls import CaptureCallCapacityError, CaptureCallRegistry, CaptureDrain
    from earshot.live import LiveConfig, LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    now = [1_000.0]
    live = LiveSessionRegistry(
        config=LiveConfig(max_sessions=1, max_sessions_per_project=4, session_ttl_seconds=1.0),
        clock=lambda: now[0],
    )
    registry = CaptureCallRegistry(live, journal_dir=capture_dir, max_calls_per_project=4)

    def make_drain(project_id: str, session_id: str) -> CaptureDrain:
        return CaptureDrain(
            project_id=project_id,
            session_id=session_id,
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=1,
            snapshots=[stats_snapshot(1100, 1100)],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    first = make_drain("tenant-a", "sess_global_durable_one")
    registry.drain(first)
    now[0] += 2.0
    live.expire()
    assert not live.contains(first.call_key, project_id="tenant-a")
    live.close()
    registry.close()

    restarted_live = LiveSessionRegistry(
        config=LiveConfig(max_sessions=1, max_sessions_per_project=4, session_ttl_seconds=1.0),
        clock=lambda: now[0],
    )
    restarted = CaptureCallRegistry(
        restarted_live,
        journal_dir=capture_dir,
        max_calls_per_project=4,
    )
    restarted.rebuild_from_disk()

    second = make_drain("tenant-b", "sess_global_durable_two")
    with pytest.raises(CaptureCallCapacityError):
        restarted.drain(second)

    restarted_live.close()
    restarted.close()


def test_an_oversized_existing_journal_is_not_loaded_and_remains_deletable(
    tmp_path, monkeypatch
) -> None:
    from earshot.live import LiveConfig, LiveSessionRegistry
    from earshot.storage import DEFAULT_PROJECT_ID

    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    with TestClient(make_app(IncidentStore(store_dir), capture_dir)) as before:
        accepted = before.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
        assert accepted.status_code == 202, accepted.text
        journal = next(capture_dir.glob("*.eck"))
        journal_size = journal.stat().st_size

    journal_identity = journal.resolve()
    original_read_bytes = Path.read_bytes

    def refuse_oversized_read(path: Path) -> bytes:
        if path.resolve() == journal_identity:
            pytest.fail("an oversized durable capture journal was read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_oversized_read)
    live = LiveSessionRegistry(config=LiveConfig(max_capture_journal_bytes=journal_size - 1))
    app = create_app(
        store=IncidentStore(store_dir),
        config=ApiConfig(token="t"),
        analyzer=analyze_incident,
        live_registry=live,
        capture_journal_dir=capture_dir,
    )

    with TestClient(app) as client:
        resumed = client.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)
        assert resumed.status_code == 503, resumed.text
        assert code(resumed) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
        assert journal.stat().st_size == journal_size
        assert client.app.state.capture_calls.drop_project(DEFAULT_PROJECT_ID) is True
        assert not journal.exists()
        assert list(capture_dir.glob("*.capture.json")) == []


def test_memory_only_capacity_rejection_can_accept_the_same_drain_after_retry() -> None:
    from earshot.capture.calls import CaptureCallRegistry, CaptureDrain
    from earshot.live import LiveCapacityError, LiveConfig, LiveSessionRegistry

    live = LiveSessionRegistry(config=LiveConfig(max_sessions_per_project=1))
    registry = CaptureCallRegistry(live)

    def make(session_id: str, sequence: int, snapshots: list[dict]) -> CaptureDrain:
        return CaptureDrain(
            project_id="tenant-a",
            session_id=session_id,
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=sequence,
            snapshots=snapshots,
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    occupying = make("sess_live_capacity_occupier", 1, [])
    assert registry.drain(occupying).replay is False
    candidate = make("sess_live_capacity_candidate", 1, [stats_snapshot(1100, 1200)])

    with pytest.raises(LiveCapacityError):
        registry.drain(candidate)
    with pytest.raises(LiveCapacityError):
        registry.drain(candidate)

    live.drop_project("tenant-a")
    retried = registry.drain(candidate)

    assert retried.replay is False
    assert retried.accepted_records > 0
    assert live.contains(candidate.call_key, project_id="tenant-a")


def test_memory_only_capture_expiry_drops_the_live_session(tmp_path) -> None:
    from earshot.live import LiveConfig, LiveSessionRegistry
    from earshot.storage import DEFAULT_PROJECT_ID

    now = [1_000.0]
    live = LiveSessionRegistry(
        config=LiveConfig(
            poll_interval_ms=3_600_000,
            stale_after_seconds=2.0,
            session_ttl_seconds=5.0,
        ),
        clock=lambda: now[0],
    )
    store = IncidentStore(tmp_path)
    app = create_app(
        store=store,
        config=ApiConfig(token="t"),
        analyzer=analyze_incident,
        live_registry=live,
    )

    with TestClient(app) as client:
        accepted = client.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
        assert accepted.status_code == 202, accepted.text
        call_id = accepted.json()["call_id"]
        now[0] += 6.0

        live.expire()

        assert not live.contains(call_id, project_id=DEFAULT_PROJECT_ID)

    store.close()


def test_interrupted_resync_drain_fences_every_other_new_sequence(tmp_path, monkeypatch) -> None:
    capture_dir = tmp_path / "capture"
    client = TestClient(
        make_app(IncidentStore(tmp_path / "store"), capture_dir), raise_server_exceptions=False
    )
    from earshot.capture import durable as capture_durable

    original_replace = capture_durable.os.replace
    replacements = 0

    def fail_commit_rename(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated commit-sidecar rename failure")
        original_replace(source, destination)

    monkeypatch.setattr("earshot.capture.durable.os.replace", fail_commit_rename)
    skipped = drain(
        3,
        [series(1)[0]],
        resync={
            "missedFromSequence": 1,
            "missedThroughSequence": 2,
            "reason": "client_buffer_overflow",
        },
    )
    interrupted = client.post("/v1/capture", json=skipped, headers=HEADERS)
    assert interrupted.status_code == 503

    different = client.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
    assert different.status_code == 409
    assert code(different) == "EARSHOT_CAPTURE_SEQUENCE_CONFLICT"

    exact_retry = client.post("/v1/capture", json=skipped, headers=HEADERS)
    assert exact_retry.status_code == 202, exact_retry.text
    assert exact_retry.json()["replayed"] is False


def test_finalized_capture_retry_remains_idempotent_after_seal_and_restart(tmp_path) -> None:
    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    before = TestClient(make_app(IncidentStore(store_dir), capture_dir))
    ended = drain(
        1,
        [series(1)[0]],
        end={"reason": "call_ended", "timestampMs": 1100},
    )
    accepted = before.post("/v1/capture", json=ended, headers=HEADERS)
    assert accepted.status_code == 202, accepted.text
    call_id = accepted.json()["call_id"]
    from earshot.capture.durable import journal_path, read_sidecar, sidecar_path

    ledger = sidecar_path(capture_dir, call_id)
    journal = journal_path(capture_dir, call_id)
    sealed = before.post(f"/v1/live/sessions/{call_id}/seal", headers=HEADERS)
    assert sealed.status_code == 201, sealed.text
    sidecar = read_sidecar(ledger)
    assert sidecar is not None and sidecar.sealed is True
    assert not journal.exists()

    before.app.state.live.close()
    before.app.state.capture_calls.close()
    after = TestClient(make_app(IncidentStore(store_dir), capture_dir))

    retry = after.post("/v1/capture", json=ended, headers=HEADERS)
    assert retry.status_code == 200, retry.text
    assert retry.json()["replayed"] is True

    late = after.post(
        "/v1/capture", json=drain(2, [stats_snapshot(1200, 1200, 10)]), headers=HEADERS
    )
    assert late.status_code == 409
    assert code(late) == "EARSHOT_CAPTURE_CALL_CLOSED"


def test_sealed_capture_replay_retention_is_bounded_and_old_ids_stay_reserved(tmp_path) -> None:
    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    client = TestClient(
        make_app(
            IncidentStore(store_dir),
            capture_dir,
            max_sealed_capture_replay_ledgers=1,
        )
    )
    from earshot.capture.durable import journal_path, sidecar_path

    calls = []
    for index in range(2):
        body = drain(
            1,
            [series(1)[0]],
            session_id=f"sess_sealed_retention_{index}",
            end={"reason": "call_ended", "timestampMs": 1100},
        )
        accepted = client.post("/v1/capture", json=body, headers=HEADERS)
        assert accepted.status_code == 202, accepted.text
        call_id = accepted.json()["call_id"]
        sealed = client.post(f"/v1/live/sessions/{call_id}/seal", headers=HEADERS)
        assert sealed.status_code == 201, sealed.text
        calls.append((body, call_id))

    assert sidecar_path(capture_dir, calls[0][1]).exists() is False
    assert sidecar_path(capture_dir, calls[1][1]).exists()
    assert len(list(capture_dir.glob("*.capture.json"))) == 1
    assert not journal_path(capture_dir, calls[0][1]).exists()
    assert not journal_path(capture_dir, calls[1][1]).exists()
    client.app.state.store.purge(calls[0][1], project_id="default")

    expired_replay = client.post("/v1/capture", json=calls[0][0], headers=HEADERS)
    assert expired_replay.status_code == 409, expired_replay.text
    assert code(expired_replay) == "EARSHOT_CAPTURE_REPLAY_EXPIRED"
    assert not journal_path(capture_dir, calls[0][1]).exists()

    retained_replay = client.post("/v1/capture", json=calls[1][0], headers=HEADERS)
    assert retained_replay.status_code == 200, retained_replay.text
    assert retained_replay.json()["replayed"] is True
    client.app.state.live.close()
    client.app.state.capture_calls.close()


def test_memory_capture_does_not_reopen_a_stored_call_after_memory_eviction(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    client = TestClient(create_app(store=store, config=ApiConfig(token="t")))
    completed = drain(
        1,
        [series(1)[0]],
        session_id="sess_memory_sealed",
        end={"reason": "call_ended", "timestampMs": 1100},
    )
    first = client.post("/v1/capture", json=completed, headers=HEADERS)
    assert first.status_code == 202, first.text
    call_id = first.json()["call_id"]
    sealed = client.post(f"/v1/live/sessions/{call_id}/seal", headers=HEADERS)
    assert sealed.status_code == 201, sealed.text

    second = client.post(
        "/v1/capture",
        json=drain(1, [series(1)[0]], session_id="sess_memory_second"),
        headers=HEADERS,
    )
    assert second.status_code == 202, second.text

    replay = client.post("/v1/capture", json=completed, headers=HEADERS)
    assert replay.status_code == 409, replay.text
    assert code(replay) == "EARSHOT_CAPTURE_REPLAY_EXPIRED"
    client.app.state.live.close()
    client.app.state.capture_calls.close()


def test_checkpoint_upload_cannot_append_to_a_browser_capture_session(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    client = TestClient(make_app(IncidentStore(tmp_path / "store"), capture_dir))
    accepted = client.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
    assert accepted.status_code == 202, accepted.text
    call_id = accepted.json()["call_id"]
    forged_continuation = b"\x00" + (2).to_bytes(4, "big")

    response = client.post(
        f"/v1/live/sessions/{call_id}/checkpoints",
        content=forged_continuation,
        headers={
            **HEADERS,
            "Content-Type": "application/vnd.earshot.checkpoint+frames",
        },
    )

    assert response.status_code == 409
    assert code(response) == "EARSHOT_CHECKPOINT_CAPTURE_OWNED"
    assert (
        client.get("/v1/live/sessions", headers=HEADERS).json()["items"][0]["last_sequence"]
        == accepted.json()["accepted_through"]
    )


def test_commit_directory_fsync_failure_keeps_the_committed_journal(tmp_path, monkeypatch) -> None:
    import os
    import stat

    from earshot.capture import durable as capture_durable

    store_dir = tmp_path / "store"
    capture_dir = tmp_path / "capture"
    client = TestClient(
        make_app(IncidentStore(store_dir), capture_dir), raise_server_exceptions=False
    )
    original_replace = capture_durable.os.replace
    original_fsync = capture_durable.os.fsync
    replacements = 0
    failed = False

    def count_replace(source, destination):
        nonlocal replacements
        replacements += 1
        original_replace(source, destination)

    def fail_commit_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if replacements == 2 and not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("simulated commit directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("earshot.capture.durable.os.replace", count_replace)
    monkeypatch.setattr("earshot.capture.durable.os.fsync", fail_commit_directory_fsync)
    body = drain(1, [series(1)[0]])
    interrupted = client.post("/v1/capture", json=body, headers=HEADERS)
    assert interrupted.status_code == 503
    assert failed is True

    from earshot.capture.durable import read_sidecar

    ledger = next(capture_dir.glob("*.capture.json"))
    sidecar = read_sidecar(ledger)
    journal = next(capture_dir.glob("*.eck"))
    committed_bytes = journal.read_bytes()
    assert sidecar is not None
    assert sidecar.applied == 1
    assert sidecar.pending_sequence is None
    assert committed_bytes

    client.app.state.live.close()
    client.app.state.capture_calls.close()
    after = TestClient(make_app(IncidentStore(store_dir), capture_dir))
    recovered = after.post("/v1/capture", json=body, headers=HEADERS)

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["replayed"] is True
    assert journal.read_bytes() == committed_bytes


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
    before.app.state.capture_calls.close()

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


def test_torn_capture_journal_fences_continuation_after_restart(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    capture_dir = tmp_path / "capture"
    before = TestClient(make_app(store, capture_dir))

    accepted = before.post("/v1/capture", json=drain(1, [series(1)[0]]), headers=HEADERS)
    assert accepted.status_code == 202, accepted.text
    call_id = accepted.json()["call_id"]
    from earshot.capture.durable import journal_path

    journal = journal_path(capture_dir, call_id)
    committed = journal.read_bytes()
    before.app.state.live.close()
    before.app.state.capture_calls.close()

    with journal.open("ab") as stream:
        stream.write(b"\x1e\x00")
    damaged = journal.read_bytes()
    assert damaged.startswith(committed)

    after = TestClient(make_app(store, capture_dir))
    continuation = after.post("/v1/capture", json=drain(2, [series(2)[1]]), headers=HEADERS)

    assert continuation.status_code == 503, continuation.text
    assert code(continuation) == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert journal.read_bytes() == damaged
    after.app.state.live.close()
    after.app.state.capture_calls.close()


def test_expired_hosted_capture_remains_retryable_after_restart(tmp_path, monkeypatch) -> None:
    from earshot.capture import calls as capture_calls
    from earshot.capture.calls import CaptureCallRegistry, CaptureDrain
    from earshot.capture.durable import journal_path, read_sidecar, sidecar_path
    from earshot.live import LiveConfig, LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    now = [1_000.0]
    correlation_key_id = "a" * 64

    def make_live() -> LiveSessionRegistry:
        return LiveSessionRegistry(
            config=LiveConfig(stale_after_seconds=2.0, session_ttl_seconds=5.0),
            clock=lambda: now[0],
        )

    def make_drain(session_id: str, sequence: int) -> CaptureDrain:
        return CaptureDrain(
            project_id="tenant-a",
            session_id=session_id,
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=sequence,
            snapshots=[stats_snapshot(1100 + sequence * 100, 1100 + sequence * 100)],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    live = make_live()
    registry = CaptureCallRegistry(
        live,
        journal_dir=capture_dir,
        max_calls_per_project=2,
        correlation_key_id=correlation_key_id,
    )
    first_drain = make_drain("sess_expiring", 1)
    first = registry.drain(first_drain)
    journal = journal_path(capture_dir, first.call_id)
    assert journal.exists()
    second = registry.drain(make_drain("sess_expiring_two", 1))

    now[0] += 6.0

    def fail_sidecar_write(_directory, _sidecar) -> None:
        raise OSError("simulated sidecar storage failure")

    monkeypatch.setattr(capture_calls, "write_sidecar", fail_sidecar_write)
    live.expire()
    assert live.contains(first.call_id, project_id="tenant-a")
    failed_expiry = read_sidecar(sidecar_path(capture_dir, first.call_id))
    assert failed_expiry is not None and failed_expiry.expired is False
    monkeypatch.undo()

    live.expire()
    assert not live.contains(first.call_id, project_id="tenant-a")
    assert journal.exists(), "expiry must retain durable capture ownership"
    expired_sidecar = read_sidecar(sidecar_path(capture_dir, first.call_id))
    assert expired_sidecar is not None and expired_sidecar.expired is True

    same_process_retry = registry.drain(first_drain)
    assert same_process_retry.replay is True
    assert same_process_retry.call_id == first.call_id

    live.close()
    registry.close()

    restarted_live = make_live()
    restarted = CaptureCallRegistry(
        restarted_live,
        journal_dir=capture_dir,
        max_calls_per_project=2,
        correlation_key_id=correlation_key_id,
    )
    restarted.rebuild_from_disk()
    assert restarted.durable_available is True
    assert restarted_live.sessions(project_id="tenant-a") == ()
    persisted_sidecar = read_sidecar(sidecar_path(capture_dir, first.call_id))
    assert persisted_sidecar is not None and persisted_sidecar.expired is True
    restarted_retry = restarted.drain(first_drain)
    assert restarted_retry.replay is True
    assert restarted_live.contains(first.call_id, project_id="tenant-a")
    assert read_sidecar(sidecar_path(capture_dir, first.call_id)).expired is True
    continued = restarted.drain(make_drain("sess_expiring", 2))
    assert continued.replay is False
    assert read_sidecar(sidecar_path(capture_dir, first.call_id)).expired is False

    from earshot.capture.calls import CaptureCallCapacityError

    with pytest.raises(CaptureCallCapacityError):
        restarted.drain(make_drain("sess_after_expiry", 1))
    assert second.call_id != first.call_id

    restarted_live.close()
    restarted.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("rejection_coverage", (("capture.stats", "member_not_allowlisted", 1),)),
        ("trace_id", "f" * 32),
        ("span_id", "e" * 16),
        ("clock_uncertainty_ms", 2.0),
    ),
)
def test_changed_bound_drain_content_conflicts_after_restart(field, value, tmp_path) -> None:
    from dataclasses import replace

    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    correlation_key_id = "a" * 64
    first = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_bound_metadata",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(("capture.stats", "member_not_allowlisted", 0),),
        resync=None,
    )
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(
        live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    accepted = registry.drain(first)
    assert accepted.replay is False
    live.close()
    registry.close()

    restored_live = LiveSessionRegistry()
    restored = CaptureCallRegistry(
        restored_live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    restored.rebuild_from_disk()

    with pytest.raises(CaptureSequenceConflictError):
        restored.drain(replace(first, **{field: value}))

    restored_live.close()
    restored.close()


@pytest.mark.parametrize(
    "changed_metadata",
    (
        {"trace_id": "f" * 32, "span_id": "e" * 16},
        {"clock_uncertainty_ms": 2.0},
    ),
)
def test_continuation_cannot_change_call_metadata_after_restart(changed_metadata, tmp_path) -> None:
    from dataclasses import replace

    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    correlation_key_id = "a" * 64
    first = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_stable_metadata",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(
        live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    registry.drain(first)
    live.close()
    registry.close()

    restored_live = LiveSessionRegistry()
    restored = CaptureCallRegistry(
        restored_live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    restored.rebuild_from_disk()
    continuation = replace(
        first,
        drain_sequence=2,
        snapshots=[stats_snapshot(1200, 1200)],
        **changed_metadata,
    )

    with pytest.raises(CaptureSequenceConflictError):
        restored.drain(continuation)

    restored_live.close()
    restored.close()


@pytest.mark.parametrize(
    "changed_metadata",
    (
        {"trace_id": "f" * 32, "span_id": "e" * 16},
        {"clock_uncertainty_ms": 2.0},
    ),
)
def test_legacy_sidecar_migrates_call_metadata_from_its_journal(changed_metadata, tmp_path) -> None:
    import json
    from dataclasses import replace

    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.capture.durable import read_sidecar, sidecar_path
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    first = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_legacy_metadata",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id="a" * 32,
        span_id="b" * 16,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[{"type": "permission_denied", "timestamp_ms": 1100}],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    accepted = registry.drain(first)
    ledger_path = sidecar_path(capture_dir, accepted.call_id)
    legacy_document = read_sidecar(ledger_path).to_json()
    legacy_document["digests"] = {"1": first.digest(version=2)}
    legacy_document["digest_versions"] = {"1": 2}
    legacy_document.pop("call_metadata_bound")
    legacy_document.pop("call_metadata_digest")
    ledger_path.write_text(json.dumps(legacy_document, sort_keys=True, separators=(",", ":")))
    live.close()
    registry.close()

    restored_live = LiveSessionRegistry()
    restored = CaptureCallRegistry(restored_live, journal_dir=capture_dir)
    restored.rebuild_from_disk()
    continuation = replace(
        first,
        drain_sequence=2,
        snapshots=[stats_snapshot(1200, 1200)],
        device_events=[],
        **changed_metadata,
    )

    with pytest.raises(CaptureSequenceConflictError):
        restored.drain(continuation)

    restored_live.close()
    restored.close()


@pytest.mark.parametrize(
    "header_updates",
    (
        {"session_id": "foreign-call", "bundle_id": "foreign-call"},
        {"journal_id": "f" * 32},
    ),
)
def test_legacy_metadata_migration_checks_the_journal_call_identity(
    header_updates, tmp_path
) -> None:
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        _legacy_call_metadata_from_journal,
    )
    from earshot.capture.durable import journal_path
    from earshot.checkpoint.framing import encode_frame, scan_frames
    from earshot.checkpoint.records import JournalOpen, decode_entry, encode_entry
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    request = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_wrong_legacy_journal_header",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(),
        resync=None,
    )
    accepted = registry.drain(request)
    journal = journal_path(capture_dir, accepted.call_id).read_bytes()
    max_frame_bytes = live.config.max_frame_bytes
    frames = scan_frames(journal, max_body_bytes=max_frame_bytes).frames
    entries = [decode_entry(frame.body) for frame in frames]
    assert isinstance(entries[0], JournalOpen)
    entries[0] = entries[0].model_copy(update=header_updates)
    foreign_journal = b"".join(
        encode_frame(sequence, encode_entry(entry), max_body_bytes=max_frame_bytes)
        for sequence, entry in enumerate(entries, start=1)
    )

    with pytest.raises(ValueError, match="header does not match"):
        _legacy_call_metadata_from_journal(
            foreign_journal,
            call_key=accepted.call_id,
            clock_domain_id=CLOCK_ID,
            max_frame_bytes=max_frame_bytes,
        )

    live.close()
    registry.close()


@pytest.mark.parametrize("previous_digest_version", [1, 2])
def test_previous_capture_digest_versions_replay_while_new_drains_bind_rejections(
    previous_digest_version, tmp_path
) -> None:
    import json
    from dataclasses import replace

    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.capture.durable import read_sidecar, sidecar_path
    from earshot.live import LiveSessionRegistry

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    first = CaptureDrain(
        project_id="tenant-a",
        session_id="sess_legacy_digest",
        capture_version=2,
        clock_domain_id=CLOCK_ID,
        clock_uncertainty_ms=1.0,
        clock_wall_origin_ms=1_700_000_000_000.0,
        trace_id=None,
        span_id=None,
        drain_sequence=1,
        snapshots=[stats_snapshot(1100, 1100)],
        device_events=[],
        coverage=(),
        rejection_coverage=(("capture.stats", "member_not_allowlisted", 1),),
        resync=None,
    )
    correlation_key_id = "a" * 64
    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(
        live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    accepted = registry.drain(first)
    ledger_path = sidecar_path(capture_dir, accepted.call_id)
    sidecar = read_sidecar(ledger_path)
    assert sidecar is not None
    legacy_document = sidecar.to_json()
    legacy_document["digests"] = {"1": first.digest(version=previous_digest_version)}
    for key in ("expired", "call_metadata_bound", "call_metadata_digest"):
        legacy_document.pop(key, None)
    if previous_digest_version == 1:
        legacy_document.pop("digest_versions", None)
        legacy_document.pop("pending_digest_version", None)
        legacy_document.pop("retry_digest_version", None)
    else:
        legacy_document["digest_versions"] = {"1": previous_digest_version}
    ledger_path.write_text(json.dumps(legacy_document, sort_keys=True, separators=(",", ":")))

    live.close()
    registry.close()

    restored_live = LiveSessionRegistry()
    restored = CaptureCallRegistry(
        restored_live,
        journal_dir=capture_dir,
        correlation_key_id=correlation_key_id,
    )
    restored.rebuild_from_disk()
    assert restored.drain(first).replay is True
    migrated = read_sidecar(ledger_path)
    assert migrated is not None
    assert migrated.call_metadata_bound is True
    assert migrated.call_metadata_digest == first.call_metadata_digest()

    next_drain = replace(
        first,
        drain_sequence=2,
        snapshots=[stats_snapshot(1200, 1200)],
        rejection_coverage=(("capture.stats", "member_not_allowlisted", 2),),
    )
    assert restored.drain(next_drain).replay is False
    changed = replace(
        next_drain,
        rejection_coverage=(("capture.stats", "member_not_allowlisted", 3),),
    )
    with pytest.raises(CaptureSequenceConflictError):
        restored.drain(changed)

    restored_live.close()
    restored.close()


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
    before.app.state.capture_calls.close()

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
    before.app.state.capture_calls.close()

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
    before.app.state.capture_calls.close()

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
                    "jitter": 0.01, "concealedSamples": 0,
                    "totalSamplesReceived": received * 10,
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


def _interrupted_drain_script(capture_dir: Path, *, phase: str) -> str:
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
                    "jitter": 0.01, "concealedSamples": 0,
                    "totalSamplesReceived": received * 10,
                    "jitterBufferDelay": received / 1000.0,
                    "jitterBufferEmittedCount": received,
                }}}},
            }}
            return CaptureDrain(
                project_id="default", session_id="sess_transaction", capture_version=2,
                clock_domain_id={CLOCK_ID!r}, clock_uncertainty_ms=1.0,
                clock_wall_origin_ms=1700000000000.0, trace_id=None, span_id=None,
                drain_sequence=seq, snapshots=[snap], device_events=[],
                coverage=(), rejection_coverage=(), resync=None, end=None,
            )

        live = LiveSessionRegistry()
        registry = CaptureCallRegistry(live, journal_dir={str(capture_dir)!r})
        first = registry.drain(make(1, 1100))
        sys.stdout.write(str(first.accepted_through))
        sys.stdout.flush()
        original = live.accept_records

        def interrupt(*args, **kwargs):
            if {phase!r} == "before_append":
                os.kill(os.getpid(), signal.SIGKILL)
            result = original(*args, **kwargs)
            if {phase!r} == "after_append":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        live.accept_records = interrupt
        registry.drain(make(2, 1200))
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )


def test_a_sigkilled_backend_leaves_a_resumable_call_on_disk(tmp_path) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
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
                    "jitter": 0.01,
                    "jitterBufferDelay": received / 1000.0,
                    "jitterBufferEmittedCount": received,
                    "concealedSamples": 0,
                    "totalSamplesReceived": received * 10,
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


@pytest.mark.parametrize("phase", ["before_append", "after_append"])
def test_sigkill_during_drain_retries_the_same_bytes_without_losing_or_duplicating_frames(
    tmp_path, phase
) -> None:
    from earshot.capture.calls import (
        CaptureCallRegistry,
        CaptureDrain,
        CaptureSequenceConflictError,
    )
    from earshot.live import LiveSessionRegistry

    def make(sequence: int, received: int) -> CaptureDrain:
        return CaptureDrain(
            project_id="default",
            session_id="sess_transaction",
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=sequence,
            snapshots=[stats_snapshot(1000 + sequence * 100, received)],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    control_live = LiveSessionRegistry()
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control = CaptureCallRegistry(control_live, journal_dir=control_dir)
    first = control.drain(make(1, 1100))
    control_live.close()

    capture_dir = tmp_path / phase
    capture_dir.mkdir()
    crashed = subprocess.run(
        [sys.executable, "-c", _interrupted_drain_script(capture_dir, phase=phase)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert crashed.returncode == -9, crashed.stderr.decode()
    assert int(crashed.stdout.decode()) == first.accepted_through

    live = LiveSessionRegistry()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    registry.rebuild_from_disk()
    with pytest.raises(CaptureSequenceConflictError):
        registry.drain(make(2, 9999))

    retried = registry.drain(make(2, 1200))
    summary = live.summary(retried.call_id, project_id="default")
    if phase == "after_append":
        # The subprocess died after the append and commit marker became durable.
        assert retried.replay is True
        assert retried.accepted_records == 0
    else:
        # The pending intent was rolled back, so the exact retry applies once.
        assert retried.replay is False
        assert retried.accepted_records > 0
    assert summary.last_sequence > first.accepted_through
    assert summary.last_sequence == retried.accepted_through
    live.close()


def test_projection_failure_discards_uncommitted_live_memory_before_exact_retry(
    tmp_path, monkeypatch
) -> None:
    import earshot.live as live_module
    from earshot.capture.calls import CaptureCallRegistry, CaptureDrain
    from earshot.live import LiveSessionRegistry

    def make(sequence: int, received: int) -> CaptureDrain:
        return CaptureDrain(
            project_id="default",
            session_id="sess_projection_failure",
            capture_version=2,
            clock_domain_id=CLOCK_ID,
            clock_uncertainty_ms=1.0,
            clock_wall_origin_ms=1_700_000_000_000.0,
            trace_id=None,
            span_id=None,
            drain_sequence=sequence,
            snapshots=[stats_snapshot(1000 + sequence * 100, received)],
            device_events=[],
            coverage=(),
            rejection_coverage=(),
            resync=None,
        )

    live = LiveSessionRegistry()
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    registry = CaptureCallRegistry(live, journal_dir=capture_dir)
    first = registry.drain(make(1, 1100))
    project_call_id = first.call_id
    original_absorb = live_module._absorb
    should_fail = True

    def fail_during_projection(session, entry) -> None:
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("simulated projection failure")
        original_absorb(session, entry)

    monkeypatch.setattr(live_module, "_absorb", fail_during_projection)
    with pytest.raises(RuntimeError, match="simulated projection failure"):
        registry.drain(make(2, 1200))

    assert not live.contains(project_call_id, project_id="default")

    retried = registry.drain(make(2, 1200))
    assert retried.replay is True
    assert retried.accepted_records == 0
    summary = live.summary(project_call_id, project_id="default")
    assert summary.last_sequence > first.accepted_through
    assert summary.last_sequence == retried.accepted_through
    live.close()
