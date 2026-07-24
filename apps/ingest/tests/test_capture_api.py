"""Gate: ``POST /v1/capture`` is an authenticated, versioned, bounded, fail-closed
transport that turns a @earshot/browser capture batch into a governed incident.

Four properties are load-bearing and each is asserted here rather than assumed:

* **Authentication and CSRF are the same ``/v1`` rules as every other endpoint.**
  A bearer project key works; a viewer session works only with its CSRF token;
  an asserted project that disagrees with the credential is refused.
* **The wire format is versioned.** An unsupported ``captureVersion`` is a clean,
  specific client error, not a schema error and not a 500.
* **Nothing is trusted.** Members outside the server's own allowlist are dropped
  before an engine sees them, so ``base64Certificate``, a DTLS ``fingerprint``,
  an ``usernameFragment``, a candidate address and a device label cannot be
  persisted -- asserted against the bytes actually served back.
* **Browser time stays in the browser's clock domain** at its raw readings, with
  no invented calibration to the server clock.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from earshot.analysis import analyze_incident
from earshot.api import CAPTURE_PROTOCOL_VERSION, ApiConfig, create_app
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

CLOCK_ID = "clk_0011223344556677"
TRACE_ID = "a" * 32
SPAN_ID = "b" * 16

# Values that must never survive the server-side allowlist. Each is a real class
# of host-identifying material a hostile or naive client could put on the wire.
CERTIFICATE_SENTINEL = "SENTINELbase64Certificate=="
FINGERPRINT_SENTINEL = "AA:BB:CC:SENTINELFINGERPRINT"
UFRAG_SENTINEL = "SENTINELufrag"
ADDRESS_SENTINEL = "203.0.113.77"
LABEL_SENTINEL = "SENTINEL Headset (Bluetooth)"


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


def payload(**overrides) -> dict:
    body = {
        "captureVersion": CAPTURE_PROTOCOL_VERSION,
        "sessionId": "sess_a1b2c3d4",
        "traceContext": {
            "traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01",
            "traceId": TRACE_ID,
            "spanId": SPAN_ID,
        },
        "clockDomain": clock_domain(),
        "snapshots": [
            {
                "timestamp_ms": 1000,
                "stats": {
                    "IT": inbound(packetsReceived=1000, packetsLost=5, jitter=0.010),
                    "T": {
                        "type": "transport",
                        "iceState": "connected",
                        "selectedCandidatePairId": "CP1",
                    },
                },
            },
            {
                "timestamp_ms": 2000,
                "stats": {
                    "IT": inbound(packetsReceived=1100, packetsLost=55, jitter=0.050),
                    "T": {
                        "type": "transport",
                        "iceState": "disconnected",
                        "selectedCandidatePairId": "CP1",
                    },
                },
            },
        ],
        "deviceEvents": [
            {
                "type": "latency",
                "timestamp_ms": 1500,
                "base_latency_s": 0.005,
                "output_latency_s": 0.02,
                "render_queue_s": 0.04,
            }
        ],
        "coverage": [
            {
                "signal": "webrtc.snapshots",
                "availability": "partial",
                "reason": "buffer_overflow_oldest_dropped",
                "droppedCount": 3,
            }
        ],
    }
    body.update(overrides)
    return body


def profile(client, bundle_id) -> dict:
    response = client.get(f"/v1/incidents/{bundle_id}")
    assert response.status_code == 200
    return json.loads(response.text)["profile"]


# -- auth, project scoping, CSRF ----------------------------------------------


def test_capture_requires_a_credential_when_authentication_is_required(tmp_path) -> None:
    config = ApiConfig(token="test-token")
    _, client = app_client(tmp_path, config=config)
    anonymous = client.post("/v1/capture", json=payload())
    assert anonymous.status_code == 401
    assert code(anonymous) == "EARSHOT_UNAUTHORIZED"

    authorized = client.post(
        "/v1/capture",
        json=payload(),
        headers={"Authorization": "Bearer test-token"},
    )
    assert authorized.status_code == 201


def test_capture_scopes_the_incident_to_the_authenticated_project(tmp_path) -> None:
    store, client = app_client(tmp_path, config=ApiConfig(token="test-token"))
    store.create_project("browser-app", display_name="Browser app")
    issued = store.issue_api_key("browser-app", label="capture key")

    accepted = client.post(
        "/v1/capture",
        json=payload(),
        headers={"Authorization": f"Bearer {issued.credential}"},
    )
    assert accepted.status_code == 201
    assert accepted.json()["project_id"] == "browser-app"

    # The default project cannot read another project's capture incident.
    other = client.get(
        f"/v1/incidents/{accepted.json()['bundle_id']}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert other.status_code == 404


def test_capture_rejects_an_asserted_project_the_credential_does_not_select(tmp_path) -> None:
    store, client = app_client(tmp_path, config=ApiConfig(token="test-token"))
    store.create_project("browser-app", display_name="Browser app")
    issued = store.issue_api_key("browser-app", label="capture key")
    response = client.post(
        "/v1/capture",
        json=payload(),
        headers={
            "Authorization": f"Bearer {issued.credential}",
            "X-Earshot-Project-Id": "default",
        },
    )
    assert response.status_code == 403
    assert code(response) == "EARSHOT_PROJECT_MISMATCH"


def test_capture_from_a_viewer_session_requires_the_csrf_token(tmp_path) -> None:
    store, client = app_client(tmp_path, config=ApiConfig(token="test-token"))
    issued = store.issue_api_key("default", label="viewer key")
    exchange = client.post(
        "/v1/auth/session",
        headers={"Authorization": f"Bearer {issued.credential}"},
    )
    assert exchange.status_code == 201
    csrf = exchange.json()["csrf_token"]

    without_csrf = client.post("/v1/capture", json=payload())
    assert without_csrf.status_code == 403
    assert code(without_csrf) == "EARSHOT_CSRF_REQUIRED"

    with_csrf = client.post(
        "/v1/capture",
        json=payload(),
        headers={"X-Earshot-CSRF": csrf},
    )
    assert with_csrf.status_code == 201


# -- versioned wire format -----------------------------------------------------


def test_unsupported_capture_version_is_a_specific_client_error(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload(captureVersion=999))
    assert response.status_code == 400
    assert code(response) == "EARSHOT_UNSUPPORTED_CAPTURE_VERSION"
    # The answer names the version this server speaks, without echoing the client's.
    assert "999" not in response.json()["error"]["message"]


@pytest.mark.parametrize("version", [None, "1", 1.0, True])
def test_capture_version_must_be_a_declared_integer(tmp_path, version) -> None:
    _, client = app_client(tmp_path)
    body = payload()
    if version is None:
        body.pop("captureVersion")
    else:
        body["captureVersion"] = version
    response = client.post("/v1/capture", json=body)
    assert response.status_code == 400
    assert code(response) == "EARSHOT_CAPTURE_VERSION_REQUIRED"


def test_version_is_checked_before_the_rest_of_the_schema(tmp_path) -> None:
    # A client on a future wire format learns that, not a pile of field errors
    # about a schema it was never targeting.
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json={"captureVersion": 2, "somethingElseEntirely": True},
    )
    assert code(response) == "EARSHOT_UNSUPPORTED_CAPTURE_VERSION"


# -- bounds: never a 500 -------------------------------------------------------


def test_body_limit_is_enforced_while_streaming(tmp_path) -> None:
    config = ApiConfig(max_capture_body_bytes=256)
    _, client = app_client(tmp_path, config=config)
    response = client.post("/v1/capture", json=payload())
    assert response.status_code == 413
    assert code(response) == "EARSHOT_BODY_TOO_LARGE"


@pytest.mark.parametrize(
    ("field", "config_field"),
    [
        ("snapshots", "max_capture_snapshots"),
        ("deviceEvents", "max_capture_device_events"),
        ("coverage", "max_capture_coverage"),
    ],
)
def test_collection_count_limits_are_explicit(tmp_path, field, config_field) -> None:
    config = ApiConfig(**{config_field: 1})
    _, client = app_client(tmp_path, config=config)
    body = payload()
    body[field] = [*body[field], *body[field]]
    response = client.post("/v1/capture", json=body)
    assert response.status_code == 413
    assert code(response) == "EARSHOT_CAPTURE_TOO_LARGE"


def test_stats_per_snapshot_limit_is_explicit(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(max_capture_stats_per_snapshot=1))
    response = client.post("/v1/capture", json=payload())
    assert response.status_code == 413
    assert code(response) == "EARSHOT_CAPTURE_TOO_LARGE"


def test_capture_config_bounds_must_be_positive() -> None:
    for field in (
        "max_capture_body_bytes",
        "max_capture_snapshots",
        "max_capture_device_events",
        "max_capture_coverage",
        "max_capture_stats_per_snapshot",
    ):
        with pytest.raises(ValueError):
            ApiConfig(**{field: 0})


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"snapshots": [{"timestamp_ms": -1, "stats": {}}]}, "EARSHOT_INVALID_CAPTURE"),
        ({"snapshots": [{"timestamp_ms": 1e18, "stats": {}}]}, "EARSHOT_INVALID_CAPTURE"),
        ({"sessionId": "a session with spaces"}, "EARSHOT_INVALID_CAPTURE"),
        ({"sessionId": "x" * 300}, "EARSHOT_INVALID_CAPTURE"),
        ({"clockDomain": clock_domain(kind="server_wall")}, "EARSHOT_INVALID_CAPTURE"),
        ({"clockDomain": clock_domain(wallOriginMs=1e18)}, "EARSHOT_INVALID_CAPTURE"),
        ({"deviceEvents": [{"timestamp_ms": 1}]}, "EARSHOT_INVALID_CAPTURE"),
        (
            {"coverage": [{"signal": "a", "availability": "sure", "reason": "b"}]},
            "EARSHOT_INVALID_CAPTURE",
        ),
        (
            {"traceContext": {"traceparent": "nope", "traceId": TRACE_ID, "spanId": SPAN_ID}},
            "EARSHOT_INVALID_CAPTURE",
        ),
    ],
)
def test_malformed_payloads_are_clean_client_errors(tmp_path, body, expected) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload(**body))
    assert response.status_code == 422
    assert code(response) == expected
    # Field paths are reported; payload values never are.
    assert all(
        item["message"] == "capture field is invalid" for item in response.json()["error"]["issues"]
    )


def test_unknown_envelope_keys_are_refused(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload(transcript="hello there"))
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_CAPTURE"


def test_non_object_and_malformed_json_bodies_are_refused(tmp_path) -> None:
    _, client = app_client(tmp_path)
    listed = client.post("/v1/capture", json=[1, 2, 3])
    assert listed.status_code == 400
    assert code(listed) == "EARSHOT_MALFORMED_CAPTURE"

    broken = client.post(
        "/v1/capture",
        content=b"{ not json",
        headers={"Content-Type": "application/json"},
    )
    assert broken.status_code == 400
    assert code(broken) == "EARSHOT_MALFORMED_JSON"

    empty = client.post(
        "/v1/capture",
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert empty.status_code == 400
    assert code(empty) == "EARSHOT_EMPTY_BODY"


def test_non_json_media_type_is_refused(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        content=b"\x00\x01",
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert response.status_code == 415
    assert code(response) == "EARSHOT_UNSUPPORTED_MEDIA_TYPE"


def test_hostile_numeric_members_do_not_reach_the_recorder(tmp_path) -> None:
    # A negative or absurd counter would make an engine derive a value outside a
    # governed domain. It is dropped at the boundary instead of raising.
    _, client = app_client(tmp_path)
    body = payload(
        snapshots=[
            {"timestamp_ms": 10, "stats": {"IT": inbound(packetsReceived=100, packetsLost=-500)}},
            {"timestamp_ms": 20, "stats": {"IT": inbound(packetsReceived=200, packetsLost=-400)}},
        ]
    )
    response = client.post("/v1/capture", json=body)
    assert response.status_code == 201
    assert response.json()["rejected_stat_members"] == 2
    samples = profile(client, response.json()["bundle_id"])["quality_samples"]
    names = {m["name"] for sample in samples for m in sample["measurements"]}
    assert "packet_loss_ratio" not in names


# -- server-side field enforcement (privacy sentinels) -------------------------


def hostile_payload() -> dict:
    return payload(
        snapshots=[
            {
                "timestamp_ms": 1000,
                "stats": {
                    "CERT": {
                        "type": "certificate",
                        "base64Certificate": CERTIFICATE_SENTINEL,
                        "fingerprint": FINGERPRINT_SENTINEL,
                    },
                    "LC": {
                        "type": "local-candidate",
                        "networkType": "wifi",
                        "address": ADDRESS_SENTINEL,
                        "ip": ADDRESS_SENTINEL,
                        "port": 51234,
                        "url": f"stun:{ADDRESS_SENTINEL}:3478",
                        "usernameFragment": UFRAG_SENTINEL,
                        "relatedAddress": ADDRESS_SENTINEL,
                    },
                    "IT": inbound(packetsReceived=10, packetsLost=0),
                },
            }
        ],
        deviceEvents=[
            {
                "type": "permission",
                "timestamp_ms": 1000,
                "state": "granted",
                "label": LABEL_SENTINEL,
                "deviceId": LABEL_SENTINEL,
                "deviceHash": LABEL_SENTINEL,
            }
        ],
    )


def test_host_identifying_members_never_reach_storage(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=hostile_payload())
    assert response.status_code == 201
    served = client.get(f"/v1/incidents/{response.json()['bundle_id']}").text
    for sentinel in (
        CERTIFICATE_SENTINEL,
        FINGERPRINT_SENTINEL,
        UFRAG_SENTINEL,
        ADDRESS_SENTINEL,
        LABEL_SENTINEL,
    ):
        assert sentinel not in served
    # And the response itself does not reflect them back either.
    assert not any(
        sentinel in response.text
        for sentinel in (CERTIFICATE_SENTINEL, ADDRESS_SENTINEL, LABEL_SENTINEL)
    )


def test_refused_members_are_counted_and_recorded_as_coverage(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=hostile_payload())
    body = response.json()
    assert body["rejected_stats"] == 1  # the certificate stat, dropped whole
    assert body["rejected_stat_members"] >= 6  # address/ip/port/url/ufrag/relatedAddress
    assert body["rejected_device_members"] == 3  # label, deviceId, malformed deviceHash
    coverage = {
        (item["signal"], item["availability"], item["reason"])
        for item in profile(client, body["bundle_id"])["coverage"]
    }
    assert ("capture.stats", "partial", "non_governed_stat_dropped") in coverage
    assert ("capture.stat_members", "partial", "non_governed_member_dropped") in coverage
    assert ("capture.device_event_members", "partial", "non_governed_member_dropped") in coverage


def test_a_stat_type_no_engine_consumes_is_dropped_whole(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(
            snapshots=[
                {
                    "timestamp_ms": 5,
                    "stats": {
                        "OUT": {"type": "outbound-rtp", "kind": "audio", "packetsSent": 10},
                        "PC": {"type": "peer-connection", "dataChannelsOpened": 1},
                    },
                }
            ]
        ),
    )
    assert response.status_code == 201
    assert response.json()["rejected_stats"] == 2


def test_an_unknown_device_event_type_is_dropped_not_stored(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(deviceEvents=[{"type": "invented_signal", "timestamp_ms": 1}]),
    )
    assert response.status_code == 201
    assert response.json()["rejected_device_events"] == 1
    assert response.json()["accepted_device_events"] == 0


# -- clock domain honesty ------------------------------------------------------


def test_browser_facts_land_in_the_browser_clock_domain_at_raw_readings(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload())
    assert response.status_code == 201
    stored = profile(client, response.json()["bundle_id"])

    domains = {item["clock_domain_id"]: item for item in stored["clock_domains"]}
    assert domains[CLOCK_ID]["kind"] == "browser_monotonic"
    assert domains[CLOCK_ID]["observer"] == "browser"
    assert domains[CLOCK_ID]["wall_origin_unix_nano"] == "1700000000000000000"
    server_domain = stored["session"]["started_at"]["clock_domain_id"]
    assert server_domain != CLOCK_ID

    reconnecting = next(
        item for item in stored["events"] if item["event_name"] == "earshot.transport.reconnecting"
    )
    assert reconnecting["time"]["clock_domain_id"] == CLOCK_ID
    # The RAW browser reading, not an offset from the batch's first timestamp.
    assert reconnecting["time"]["monotonic_time_nano"] == "2000000000"
    # No calibration between the browser and server clocks is invented.
    assert stored.get("clock_relations", []) == []


def test_a_batch_without_a_wall_origin_carries_only_the_monotonic_reading(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(clockDomain=clock_domain(wallOriginMs=None)),
    )
    assert response.status_code == 201
    stored = profile(client, response.json()["bundle_id"])
    reconnecting = next(
        item for item in stored["events"] if item["event_name"] == "earshot.transport.reconnecting"
    )
    assert reconnecting["time"]["monotonic_time_nano"] == "2000000000"
    assert reconnecting["time"].get("source_time_unix_nano") is None


# -- coverage, facts, and idempotent delivery ----------------------------------


def test_client_coverage_is_recorded_under_its_own_observer_namespace(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload())
    coverage = {
        (item["signal"], item["availability"], item["reason"])
        for item in profile(client, response.json()["bundle_id"])["coverage"]
    }
    assert ("browser.webrtc.snapshots", "partial", "buffer_overflow_oldest_dropped") in coverage


def test_a_client_cannot_mask_a_server_derived_coverage_note(tmp_path) -> None:
    # A payload claiming a signal is "available" must not overwrite what the
    # engine actually observed; the client's claim is namespaced to the browser.
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(
            coverage=[
                {"signal": "client.render", "availability": "available", "reason": "trust_me"}
            ]
        ),
    )
    coverage = {
        item["signal"]: item for item in profile(client, response.json()["bundle_id"])["coverage"]
    }
    assert coverage["client.render"]["availability"] == "not_observed"
    assert coverage["browser.client.render"]["availability"] == "available"


def test_capture_batch_becomes_analyzable_governed_facts(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/capture", json=payload())
    bundle_id = response.json()["bundle_id"]
    stored = profile(client, bundle_id)

    names = {
        measurement["name"]
        for sample in stored["quality_samples"]
        for measurement in sample["measurements"]
    }
    assert {
        "packet_loss_ratio",
        "jitter",
        "audio.base_latency",
        "audio.render_queue_delay",
    } <= names

    analysis = client.get(f"/v1/incidents/{bundle_id}/analysis")
    assert analysis.status_code == 200
    codes = {item["code"] for item in analysis.json()["analysis"]["diagnoses"]}
    assert {"network.degraded", "transport.reconnect"} <= codes


def test_playout_stats_make_a_render_underrun_observable(tmp_path) -> None:
    # RTCAudioPlayoutStats: synthesized samples are audio the output device had
    # to invent because the render queue ran dry -- an observed render fault.
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(
            snapshots=[
                {
                    "timestamp_ms": 1000,
                    "stats": {
                        "PL": {
                            "type": "media-playout",
                            "kind": "audio",
                            "totalPlayoutDelay": 1.0,
                            "totalSamplesCount": 48000,
                            "synthesizedSamplesDuration": 0.0,
                            "totalSamplesDuration": 1.0,
                        }
                    },
                },
                {
                    "timestamp_ms": 2000,
                    "stats": {
                        "PL": {
                            "type": "media-playout",
                            "kind": "audio",
                            "totalPlayoutDelay": 2.5,
                            "totalSamplesCount": 96000,
                            "synthesizedSamplesDuration": 0.2,
                            "totalSamplesDuration": 2.0,
                        }
                    },
                },
            ]
        ),
    )
    bundle_id = response.json()["bundle_id"]
    stored = profile(client, bundle_id)
    names = {
        measurement["name"]
        for sample in stored["quality_samples"]
        for measurement in sample["measurements"]
    }
    assert {"playout_delay", "synthesized_samples_ratio"} <= names
    analysis = client.get(f"/v1/incidents/{bundle_id}/analysis")
    codes = {item["code"] for item in analysis.json()["analysis"]["diagnoses"]}
    assert "audio.stale_playback" in codes


def test_redelivering_a_batch_resolves_to_the_same_incident(tmp_path) -> None:
    _, client = app_client(tmp_path)
    first = client.post("/v1/capture", json=payload())
    second = client.post("/v1/capture", json=payload())
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["bundle_id"] == first.json()["bundle_id"]
    listed = client.get("/v1/incidents", params={"session_id": "sess_a1b2c3d4"})
    assert len(listed.json()["items"]) == 1


def test_a_different_batch_is_a_different_incident(tmp_path) -> None:
    _, client = app_client(tmp_path)
    first = client.post("/v1/capture", json=payload())
    later = payload()
    later["snapshots"][1]["timestamp_ms"] = 3000
    second = client.post("/v1/capture", json=later)
    assert second.status_code == 201
    assert second.json()["bundle_id"] != first.json()["bundle_id"]


def test_an_empty_batch_is_accepted_and_records_no_fabricated_facts(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/capture",
        json=payload(snapshots=[], deviceEvents=[], coverage=[]),
    )
    assert response.status_code == 201
    stored = profile(client, response.json()["bundle_id"])
    assert stored["events"] == []
    assert stored["quality_samples"] == []


def test_capture_response_reports_the_accepted_and_refused_counts(tmp_path) -> None:
    _, client = app_client(tmp_path)
    body = client.post("/v1/capture", json=payload()).json()
    assert body["capture_version"] == CAPTURE_PROTOCOL_VERSION
    assert body["trace_id"] == TRACE_ID
    assert body["accepted_snapshots"] == 2
    assert body["accepted_device_events"] == 1
    assert body["accepted_coverage"] == 1
    assert body["framework"] == "browser_capture"


def test_capture_is_published_in_the_openapi_contract(tmp_path) -> None:
    _, client = app_client(tmp_path)
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/capture"]["post"]
    assert operation["security"] == [{"BearerAuth": []}, {"BrowserSession": []}, {}]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/CaptureRequest"}
    assert "captureVersion" in schema["components"]["schemas"]["CaptureRequest"]["properties"]


def test_concurrent_delivery_of_one_batch_yields_one_incident(tmp_path) -> None:
    # Two deliveries of the same batch race: the loser finds the identifier taken
    # by evidence identical to its own, which is the same incident, not a conflict.
    _, client = app_client(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [
            future.result()
            for future in [
                pool.submit(client.post, "/v1/capture", json=payload()) for _ in range(2)
            ]
        ]
    assert {response.status_code for response in responses} <= {200, 201}
    assert len({response.json()["bundle_id"] for response in responses}) == 1
    assert len(client.get("/v1/incidents").json()["items"]) == 1
