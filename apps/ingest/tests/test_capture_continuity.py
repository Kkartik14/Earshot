"""Gate: ``captureVersion: 2`` makes one browser call one continuous artifact.

Where ``captureVersion: 1`` turns every drain into its own incident, version 2
accumulates a whole call into one journal-backed, **provisional** live session:

* Thirty drains of one call become **one** live session, and sealing it yields a
  single provisional artifact whose governed facts are byte-identical to running
  the whole concatenated series through the engines once -- the Phase 0
  fold-equivalence guarantee, now visible in the artifact.
* Idempotency is by slot *and* content: a re-sent drain resolves to what it
  already produced (``200``, no second application), a forged drain at a taken
  slot is refused (``409`` conflict), a gap with no declared loss is refused
  (``409`` gap naming the expected sequence), and a gap the client honestly
  declares is applied and ledgered as coverage.
* A reconnect whose ``disconnected`` and ``connected`` snapshots fall in
  different drains is reported rather than silently lost at the boundary.
* Two tenants using the same ``sessionId`` never share a call: the call key folds
  in the authenticated project, so identical bodies produce two isolated calls.
* The call is visible and tailable on the live surface, and a mid-call seal is
  provisional under a ``.s{sequence}`` bundle id -- it can never finalize on a
  timer, a tab close, or a TTL.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from earshot.analysis import analyze_incident
from earshot.api import ApiConfig, create_app
from earshot.contract import IncidentBundle
from earshot.storage import IncidentStore
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

CLOCK_ID = "clk_0011223344556677"
TRACE_ID = "a" * 32
SPAN_ID = "b" * 16


def app_client(tmp_path, *, config: ApiConfig | None = None):
    store = IncidentStore(tmp_path)
    app = create_app(store=store, config=config, analyzer=analyze_incident)
    return store, TestClient(app)


def code(response) -> str:
    return response.json()["error"]["code"]


def clock_domain(**overrides) -> dict:
    return {
        "id": CLOCK_ID,
        "kind": "browser_monotonic",
        "unit": "ms",
        "uncertaintyMs": 1,
        "wallOriginMs": 1_700_000_000_000,
        **overrides,
    }


def inbound(**members) -> dict:
    return {"type": "inbound-rtp", "kind": "audio", **members}


def transport(state: str = "connected") -> dict:
    return {"type": "transport", "iceState": state, "selectedCandidatePairId": "P"}


def stats_snapshot(ts: float, received: int, lost: int, **extra_stats) -> dict:
    """One ``getStats`` snapshot rich enough to yield loss/jitter-buffer deltas."""

    stats = {
        "IT": inbound(
            packetsReceived=received,
            packetsLost=lost,
            jitter=0.01,
            jitterBufferDelay=received / 1000.0,
            jitterBufferEmittedCount=received,
            concealedSamples=lost,
            totalSamplesReceived=received * 10,
        ),
        **extra_stats,
    }
    return {"timestamp_ms": ts, "stats": stats}


def drain(sequence: int, snapshots: list[dict], *, session_id: str = "sess_a1b2c3d4", **extra):
    body = {
        "captureVersion": 2,
        "sessionId": session_id,
        "clockDomain": clock_domain(),
        "drainSequence": sequence,
        "capturerStartedAtMs": 0,
        "snapshots": snapshots,
        **extra,
    }
    return body


def series(count: int) -> list[dict]:
    """A monotonically-growing snapshot series, one snapshot per drain."""

    return [
        stats_snapshot(1000 + index * 100, 1000 + index * 100, 5 * index) for index in range(count)
    ]


def measurement_facts(bundle: IncidentBundle) -> Counter:
    facts: Counter = Counter()
    for sample in bundle.profile.quality_samples:
        for measurement in sample.measurements:
            facts[
                (
                    measurement.name,
                    round(measurement.value, 9),
                    measurement.unit,
                    sample.sample_window.start.monotonic_time_nano,
                )
            ] += 1
    return facts


def event_facts(bundle: IncidentBundle) -> Counter:
    return Counter(
        (event.event_name, event.time.monotonic_time_nano) for event in bundle.profile.events
    )


def seal(client, headers, call_id: str) -> dict:
    return client.post(f"/v1/live/sessions/{call_id}/seal", headers=headers).json()


def fetch_bundle(client, headers, bundle_id: str) -> IncidentBundle:
    response = client.get(f"/v1/incidents/{bundle_id}", headers=headers)
    assert response.status_code == 200, response.text
    return IncidentBundle.model_validate(response.json())


# -- one call, one artifact ---------------------------------------------------


def test_many_drains_become_one_incident(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snapshots = series(31)

    call_id = None
    for index, snapshot in enumerate(snapshots):
        response = client.post("/v1/capture", json=drain(index + 1, [snapshot]), headers=headers)
        assert response.status_code == 202, response.text
        call_id = response.json()["call_id"]

    # Exactly one live session carries the whole call.
    listed = client.get("/v1/live/sessions", headers=headers).json()["items"]
    assert len(listed) == 1
    assert listed[0]["session_id"] == call_id

    sealed = seal(client, headers, call_id)
    many = fetch_bundle(client, headers, sealed["bundle_id"])

    # A single drain carrying the whole series is the reference: fold-equivalence
    # makes the accumulated facts identical to the one-shot engine run.
    _, one_client = app_client(tmp_path / "ref", config=ApiConfig(token="t"))
    one = one_client.post(
        "/v1/capture", json=drain(1, snapshots, session_id="sess_reference"), headers=headers
    ).json()["call_id"]
    one_sealed = seal(one_client, headers, one)
    single = fetch_bundle(one_client, headers, one_sealed["bundle_id"])

    assert measurement_facts(many) == measurement_facts(single)
    assert event_facts(many) == event_facts(single)
    assert validate_incident(many).ok


def test_the_assembled_incident_is_provisional_and_never_final(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    for index, snapshot in enumerate(series(10)):
        response = client.post("/v1/capture", json=drain(index + 1, [snapshot]), headers=headers)
    call_id = response.json()["call_id"]

    sealed = seal(client, headers, call_id)
    assert sealed["finality"] == "provisional"
    assert sealed["close_observed"] is False
    bundle = fetch_bundle(client, headers, sealed["bundle_id"])
    manifest = bundle.profile.manifest
    assert manifest.finality == "provisional"
    assert bundle.profile.session.ended_at is None
    assert manifest.recovery is not None
    assert manifest.recovery.method == "browser_capture_journal"
    assert manifest.recovery.close_observed is False
    assert validate_incident(bundle).ok


# -- idempotency by slot and content ------------------------------------------


def test_a_retried_drain_is_applied_once(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(3)
    for index in range(2):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)

    first = client.post("/v1/capture", json=drain(2, [snaps[1]]), headers=headers)
    assert first.status_code == 200
    assert first.json()["replayed"] is True
    accepted_through = first.json()["accepted_through"]

    again = client.post("/v1/capture", json=drain(2, [snaps[1]]), headers=headers)
    assert again.status_code == 200
    assert again.json()["accepted_through"] == accepted_through


def test_a_forged_drain_cannot_take_a_used_slot(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(3)
    for index in range(2):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)

    forged = client.post(
        "/v1/capture",
        json=drain(2, [stats_snapshot(1100, 9999, 999)]),
        headers=headers,
    )
    assert forged.status_code == 409
    assert code(forged) == "EARSHOT_CAPTURE_SEQUENCE_CONFLICT"


def test_an_out_of_order_drain_is_refused_with_the_expected_sequence(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(6)
    for index in range(3):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)

    ahead = client.post("/v1/capture", json=drain(5, [snaps[4]]), headers=headers)
    assert ahead.status_code == 409
    assert code(ahead) == "EARSHOT_CAPTURE_SEQUENCE_GAP"
    assert ahead.json()["error"]["issues"][0]["message"] == "4"


def test_a_declared_drain_loss_is_accepted_and_ledgered(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(6)
    for index in range(3):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)

    # Drain 4 was permanently lost; drain 5 declares it and is applied.
    accepted = client.post(
        "/v1/capture",
        json=drain(
            5,
            [snaps[4]],
            resync={
                "missedFromSequence": 4,
                "missedThroughSequence": 4,
                "reason": "drains_lost_in_transport",
            },
        ),
        headers=headers,
    )
    assert accepted.status_code == 202, accepted.text

    call_id = accepted.json()["call_id"]
    bundle = fetch_bundle(client, headers, seal(client, headers, call_id)["bundle_id"])
    coverage = {note.signal: note for note in bundle.profile.coverage}
    assert "capture.drain_sequence" in coverage
    assert coverage["capture.drain_sequence"].availability == "partial"
    assert coverage["capture.drain_sequence"].dropped_count == 1
    assert coverage["capture.stats_continuity"].reason == "carry_invalidated_by_drain_loss"


def test_resync_clips_a_range_already_applied_by_an_unknown_outcome(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snapshots = series(3)
    first = client.post("/v1/capture", json=drain(1, [snapshots[0]]), headers=headers)
    assert first.status_code == 202, first.text

    # The client lost the response to drain 1, then abandoned drain 2. It can
    # only claim [1, 2], while the server knows drain 1 was already committed.
    resumed = client.post(
        "/v1/capture",
        json=drain(
            3,
            [snapshots[2]],
            resync={
                "missedFromSequence": 1,
                "missedThroughSequence": 2,
                "reason": "upload_failed_payload_dropped",
            },
        ),
        headers=headers,
    )
    assert resumed.status_code == 202, resumed.text

    call_id = resumed.json()["call_id"]
    sealed = seal(client, headers, call_id)
    bundle = fetch_bundle(client, headers, sealed["bundle_id"])
    coverage = {note.signal: note for note in bundle.profile.coverage}
    assert coverage["capture.drain_sequence"].dropped_count == 1


# -- boundary reconnect -------------------------------------------------------


def test_a_boundary_reconnect_is_observed(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}

    # A connected snapshot, then a disconnected snapshot in a later drain, then a
    # connected snapshot in the drain after -- the reconnect straddles two drain
    # boundaries and must survive them.
    client.post(
        "/v1/capture",
        json=drain(1, [stats_snapshot(1000, 1000, 0, T=transport("connected"))]),
        headers=headers,
    )
    client.post(
        "/v1/capture",
        json=drain(2, [stats_snapshot(1100, 1100, 0, T=transport("disconnected"))]),
        headers=headers,
    )
    last = client.post(
        "/v1/capture",
        json=drain(3, [stats_snapshot(1200, 1200, 0, T=transport("connected"))]),
        headers=headers,
    )
    call_id = last.json()["call_id"]

    bundle = fetch_bundle(client, headers, seal(client, headers, call_id)["bundle_id"])
    assert any(
        event.event_name == "earshot.transport.reconnecting" for event in bundle.profile.events
    )
    analysis = analyze_incident(bundle, input_sha256="0" * 64, generated_at_unix_nano=0)
    assert any(diagnosis.code == "transport.reconnect" for diagnosis in analysis.diagnoses)


# -- tenant isolation ---------------------------------------------------------


def test_two_tenants_with_the_same_session_id_never_share_a_call(tmp_path) -> None:
    store, client = app_client(tmp_path, config=ApiConfig(token="t"))
    store.create_project("tenant-a", display_name="A")
    store.create_project("tenant-b", display_name="B")
    key_a = store.issue_api_key("tenant-a", label="a").credential
    key_b = store.issue_api_key("tenant-b", label="b").credential

    body = drain(1, series(3)[:1])
    accepted_a = client.post("/v1/capture", json=body, headers={"Authorization": f"Bearer {key_a}"})
    accepted_b = client.post("/v1/capture", json=body, headers={"Authorization": f"Bearer {key_b}"})
    assert accepted_a.status_code == 202
    assert accepted_b.status_code == 202
    call_a = accepted_a.json()["call_id"]
    call_b = accepted_b.json()["call_id"]
    # Identical bodies, different authenticated projects, different call keys.
    assert call_a != call_b

    # Each tenant sees only its own call, and neither can seal the other's.
    listed_a = client.get("/v1/live/sessions", headers={"Authorization": f"Bearer {key_a}"}).json()[
        "items"
    ]
    assert [item["session_id"] for item in listed_a] == [call_a]
    cross = client.post(
        f"/v1/live/sessions/{call_a}/seal", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert cross.status_code == 404


# -- live surface -------------------------------------------------------------


def test_a_capture_call_is_visible_and_tailable_on_the_live_surface(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    call_id = None
    for index, snapshot in enumerate(series(4)):
        response = client.post("/v1/capture", json=drain(index + 1, [snapshot]), headers=headers)
        call_id = response.json()["call_id"]

    listed = client.get("/v1/live/sessions", headers=headers).json()["items"]
    assert [item["session_id"] for item in listed] == [call_id]
    assert listed[0]["sealable"] is True
    assert listed[0]["close_observed"] is False

    with (
        _serve(client.app) as base,
        _open(
            f"{base}/v1/live/sessions/{call_id}/tail?from=start",
            headers={"Authorization": "Bearer t"},
        ) as response,
    ):
        events = _events(response, wanted=2)
    names = [event.get("event") for event in events]
    assert names[0] == "open"
    assert "record" in names


def test_sealing_mid_call_yields_a_provisional_artifact_under_a_sequence_suffix(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    for index, snapshot in enumerate(series(5)):
        response = client.post("/v1/capture", json=drain(index + 1, [snapshot]), headers=headers)
    call_id = response.json()["call_id"]
    accepted_through = response.json()["accepted_through"]

    sealed = seal(client, headers, call_id)
    assert sealed["bundle_id"] == f"{call_id}.s{accepted_through}"
    assert sealed["finality"] == "provisional"

    # The call is still live after a mid-call seal: nothing finalized it.
    still_live = client.get("/v1/live/sessions", headers=headers).json()["items"]
    assert [item["session_id"] for item in still_live] == [call_id]


# -- Phase 2: observed close and honest duration ------------------------------


def endcall(sequence: int, snapshots: list[dict], ts: float, **extra) -> dict:
    """A final drain that declares an explicit application-observed close."""

    return drain(sequence, snapshots, end={"reason": "call_ended", "timestampMs": ts}, **extra)


def named_measurements(bundle: IncidentBundle, name: str) -> list:
    return [
        (sample, measurement)
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name == name
    ]


def test_a_declared_call_end_produces_a_final_incident_with_a_real_duration(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(5)
    for index in range(4):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)

    # The call ends at a real browser coordinate after the last snapshot (t=1400).
    ended_at_ms = 5000.0
    response = client.post("/v1/capture", json=endcall(5, [snaps[4]], ended_at_ms), headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["finalized"] is True
    assert body["state"] == "finalized"
    call_id = body["call_id"]

    sealed = seal(client, headers, call_id)
    assert sealed["finality"] == "final"
    assert sealed["completeness"] == "complete"
    assert sealed["close_observed"] is True
    # A finalized call keeps its bundle id -- no ``.s{seq}`` provisional suffix.
    assert sealed["bundle_id"] == call_id

    bundle = fetch_bundle(client, headers, sealed["bundle_id"])
    manifest = bundle.profile.manifest
    assert manifest.finality == "final"
    assert manifest.completeness == "complete"
    # A finalized replay carries no recovery declaration at all.
    assert manifest.recovery is None

    ended = bundle.profile.session.ended_at
    assert ended is not None
    # ``session.ended_at`` is the endCall's observed browser coordinate, in the
    # browser clock domain -- never the last snapshot, never a server reading.
    assert ended.clock_domain_id == CLOCK_ID
    assert int(ended.monotonic_time_nano) == int(ended_at_ms * 1_000_000)

    duration = named_measurements(bundle, "client.observed_call_duration")
    assert len(duration) == 1
    sample, measurement = duration[0]
    # The duration is a same-clock-domain difference: endCall - first observed
    # coordinate (t=1000), both raw browser readings.
    assert measurement.value == pytest.approx(ended_at_ms - 1000.0)
    assert measurement.unit == "ms"
    assert sample.sample_window.start.clock_domain_id == CLOCK_ID
    # The two bounding events frame the call in the browser clock domain.
    names = {event.event_name for event in bundle.profile.events}
    assert "earshot.client.capture_started" in names
    assert "earshot.client.call_ended" in names
    assert validate_incident(bundle).ok


def test_an_ended_call_with_lost_drains_is_final_but_incomplete(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(6)
    for index in range(3):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)
    # Drain 4 is permanently lost; drain 5 declares it, then the call ends.
    client.post(
        "/v1/capture",
        json=drain(
            5,
            [snaps[4]],
            resync={
                "missedFromSequence": 4,
                "missedThroughSequence": 4,
                "reason": "drains_lost_in_transport",
            },
        ),
        headers=headers,
    )
    response = client.post("/v1/capture", json=endcall(6, [snaps[5]], 6000.0), headers=headers)
    assert response.json()["finalized"] is True
    call_id = response.json()["call_id"]

    sealed = seal(client, headers, call_id)
    # An observed close is still final -- but a call that lost a drain can never
    # present as complete.
    assert sealed["finality"] == "final"
    assert sealed["completeness"] == "incomplete"
    bundle = fetch_bundle(client, headers, sealed["bundle_id"])
    assert bundle.profile.manifest.completeness == "incomplete"
    assert bundle.profile.session.ended_at is not None
    coverage = {note.signal for note in bundle.profile.coverage}
    assert "capture.drain_sequence" in coverage
    assert validate_incident(bundle).ok


def test_a_closed_tab_never_becomes_a_finished_call(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    # Twenty drains, then the tab simply closes: no ``end`` ever arrives.
    for index, snapshot in enumerate(series(20)):
        response = client.post("/v1/capture", json=drain(index + 1, [snapshot]), headers=headers)
    call_id = response.json()["call_id"]

    sealed = seal(client, headers, call_id)
    assert sealed["finality"] == "provisional"
    assert sealed["close_observed"] is False
    bundle = fetch_bundle(client, headers, sealed["bundle_id"])
    manifest = bundle.profile.manifest
    assert manifest.finality == "provisional"
    # No close was observed, so there is no end and no call duration -- only the
    # extent the observer actually saw, stated on the recovery declaration.
    assert bundle.profile.session.ended_at is None
    assert named_measurements(bundle, "client.observed_call_duration") == []
    assert manifest.recovery is not None
    assert manifest.recovery.close_observed is False
    assert manifest.recovery.first_observation is not None
    assert manifest.recovery.last_observation is not None
    assert int(manifest.recovery.first_observation.monotonic_time_nano) < int(
        manifest.recovery.last_observation.monotonic_time_nano
    )
    assert validate_incident(bundle).ok


def test_stop_and_pagehide_are_not_a_call_end(tmp_path) -> None:
    for reason in ("capture_stopped", "page_hidden", "page_unloaded"):
        _, client = app_client(tmp_path / reason, config=ApiConfig(token="t"))
        headers = {"Authorization": "Bearer t"}
        snaps = series(4)
        for index in range(3):
            client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)
        # A lifecycle flush declares an abandon reason, never ``call_ended``.
        response = client.post(
            "/v1/capture",
            json=drain(4, [snaps[3]], end={"reason": reason, "timestampMs": 9000.0}),
            headers=headers,
        )
        assert response.status_code == 202, response.text
        # It does not finalize: the call stays live and sealable.
        assert response.json()["finalized"] is False
        assert response.json()["state"] != "finalized"
        call_id = response.json()["call_id"]
        still_live = client.get("/v1/live/sessions", headers=headers).json()["items"]
        assert [item["session_id"] for item in still_live] == [call_id]

        sealed = seal(client, headers, call_id)
        assert sealed["finality"] == "provisional"
        bundle = fetch_bundle(client, headers, sealed["bundle_id"])
        assert bundle.profile.session.ended_at is None
        assert named_measurements(bundle, "client.observed_call_duration") == []
        # An abandon states how far the observer saw and that the close was unseen.
        assert named_measurements(bundle, "client.observed_capture_extent")
        coverage = {note.signal: note for note in bundle.profile.coverage}
        assert coverage["client.call_duration"].reason == "close_not_observed"
        assert validate_incident(bundle).ok


def test_duration_is_never_computed_across_clock_domains(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(3)
    for index in range(2):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)
    response = client.post("/v1/capture", json=endcall(3, [snaps[2]], 4000.0), headers=headers)
    call_id = response.json()["call_id"]
    bundle = fetch_bundle(client, headers, seal(client, headers, call_id)["bundle_id"])

    # No calibration was declared, so no relation aligns the browser and server
    # clocks -- the analyzer must keep refusing cross-clock latency.
    assert bundle.profile.clock_relations == ()
    session = bundle.profile.session
    # The session's start is a server-clock reading and its end a browser one: two
    # different domains, so their difference is never taken. The honest duration is
    # the same-domain browser measurement instead.
    assert session.ended_at is not None
    assert session.ended_at.clock_domain_id == CLOCK_ID
    assert session.started_at.clock_domain_id != session.ended_at.clock_domain_id
    (sample, _measurement) = named_measurements(bundle, "client.observed_call_duration")[0]
    assert sample.sample_window.start.clock_domain_id == CLOCK_ID
    assert validate_incident(bundle).ok


def test_a_drain_after_the_close_is_refused(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    snaps = series(4)
    for index in range(2):
        client.post("/v1/capture", json=drain(index + 1, [snaps[index]]), headers=headers)
    ended = client.post("/v1/capture", json=endcall(3, [snaps[2]], 4000.0), headers=headers)
    assert ended.json()["finalized"] is True

    late = client.post("/v1/capture", json=drain(4, [snaps[3]]), headers=headers)
    assert late.status_code == 409
    assert code(late) == "EARSHOT_CAPTURE_CALL_CLOSED"

    # A retry of the endCall drain itself is still an idempotent replay, not a
    # refusal: the client that never saw the ack can safely resend it.
    retry = client.post("/v1/capture", json=endcall(3, [snaps[2]], 4000.0), headers=headers)
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["finalized"] is True


def test_an_end_declaration_requires_capture_version_2(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="t"))
    headers = {"Authorization": "Bearer t"}
    body = {
        "captureVersion": 1,
        "sessionId": "sess_a1b2c3d4",
        "clockDomain": clock_domain(),
        "snapshots": series(1),
        "end": {"reason": "call_ended", "timestampMs": 2000.0},
    }
    response = client.post("/v1/capture", json=body, headers=headers)
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_CAPTURE"


# -- a real loopback listener for the streaming assertion ---------------------


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


@contextlib.contextmanager
def _serve(app) -> Iterator[str]:
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise AssertionError("capture tail server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _open(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5.0):
    import urllib.request

    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=timeout)


def _events(response, wanted: int, *, timeout: float = 5.0) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    while len(events) < wanted and time.monotonic() < deadline:
        try:
            raw = response.readline()
        except (TimeoutError, OSError):
            break
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line == "":
            if current:
                events.append(current)
                current = {}
        else:
            key, _, value = line.partition(":")
            current[key.strip()] = value.lstrip()
    return events
