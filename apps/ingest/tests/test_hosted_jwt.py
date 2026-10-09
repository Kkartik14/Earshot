from __future__ import annotations

import dataclasses
import ipaddress
import json
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

import earshot.auth as hosted_auth
from earshot.api import ApiConfig, create_app
from earshot.auth import HostedJwtVerifier, InvalidHostedToken
from earshot.capture.durable import (
    SIDECAR_SUFFIX,
    CaptureSidecar,
    journal_path,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from earshot.checkpoint import CheckpointConfig, CheckpointWriter
from earshot.codec import decode_incident_protobuf, encode_incident_json
from earshot.live import END_PROJECT_DELETED, EVENT_END, LiveConfig, LiveSessionRegistry
from earshot.recorder import IncidentRecorder
from earshot.storage import IncidentStore, StorageCleanupPendingError
from incident_factory import SECRET_SENTINEL, make_valid_bundle


class _JwksHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        with self.server.jwks_request_lock:  # type: ignore[attr-defined]
            self.server.jwks_requests += 1  # type: ignore[attr-defined]
            request_number = self.server.jwks_requests  # type: ignore[attr-defined]
        if request_number > 1 and self.server.block_jwks_refresh:  # type: ignore[attr-defined]
            self.server.refresh_started.set()  # type: ignore[attr-defined]
            self.server.release_refresh.wait(timeout=10)  # type: ignore[attr-defined]
        body = self.server.jwks_body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def issuer(tmp_path):
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": "issuer-key-1", "alg": "RS256", "use": "sig"})

    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(tls_key, hashes.SHA256())
    )
    cert_path = tmp_path / "jwks-test-cert.pem"
    key_path = tmp_path / "jwks-test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    server.daemon_threads = True
    server.jwks_body = json.dumps({"keys": [jwk]}).encode()  # type: ignore[attr-defined]
    server.jwks_request_lock = threading.Lock()  # type: ignore[attr-defined]
    server.jwks_requests = 0  # type: ignore[attr-defined]
    server.block_jwks_refresh = False  # type: ignore[attr-defined]
    server.refresh_started = threading.Event()  # type: ignore[attr-defined]
    server.release_refresh = threading.Event()  # type: ignore[attr-defined]
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "private_key": signing_key,
            "url": f"https://127.0.0.1:{server.server_port}/jwks",
            "ca_file": str(cert_path),
            "server": server,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _token(
    issuer,
    *,
    project_id: str,
    scope: str = "earshot:read",
    kid: str = "issuer-key-1",
    **overrides: object,
) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://platform.example.test",
        "aud": "earshot-api",
        "sub": "user-123",
        "project_id": project_id,
        "scope": scope,
        "iat": now,
        "exp": now + 120,
        "jti": "token-123",
        **overrides,
    }
    return jwt.encode(
        claims,
        issuer["private_key"],
        algorithm="RS256",
        headers={"kid": kid},
    )


def _hosted_config(issuer) -> ApiConfig:
    return ApiConfig(
        auth_mode="hosted_jwt",
        jwt_issuer="https://platform.example.test",
        jwt_audience="earshot-api",
        jwks_url=issuer["url"],
        jwks_ca_file=issuer["ca_file"],
        hosted_runtime_names=frozenset({"voice-runtime"}),
        hosted_session_statuses=frozenset({"completed"}),
        hosted_event_names=frozenset({"runtime.call.started"}),
    )


def _hosted_metadata_body(*, include_raw_otlp: bool = False) -> dict[str, object]:
    encoded = json.loads(
        encode_incident_json(
            make_valid_bundle(
                bundle_id="operator-only-placeholder",
                include_raw_otlp=include_raw_otlp,
            )
        )
    )
    profile = encoded["profile"]
    manifest = profile["manifest"]
    session = profile["session"]
    session_id = session["session_id"]

    def hosted_time(time_point: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in time_point.items()
            if key in {"source_time_unix_nano", "observed_time_unix_nano"}
        }

    body: dict[str, object] = {
        "profile": {
            "manifest": {
                "schema_version": manifest["schema_version"],
                "semantic_profile_version": manifest["semantic_profile_version"],
                "session_id": session_id,
                "created_at_unix_nano": manifest["created_at_unix_nano"],
                "producer": {"name": "voice-runtime", "version": "1.2.0"},
            },
            "session": {
                "session_id": session_id,
                "status": session["status"],
                "started_at": hosted_time(session["started_at"]),
                "ended_at": (
                    None if session["ended_at"] is None else hosted_time(session["ended_at"])
                ),
            },
            "events": [
                {
                    "event_id": "event-hosted-call-started",
                    "session_id": session_id,
                    "event_name": "runtime.call.started",
                    "time": hosted_time(profile["events"][0]["time"]),
                }
            ],
            "runtime_session_id": "tvic-session-01J",
        }
    }
    if include_raw_otlp:
        body["raw_otlp_chunks"] = encoded["raw_otlp_chunks"]
    return body


def test_hosted_jwt_uses_signed_project_and_operation_scope(tmp_path, issuer) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("project-a", display_name="Project A")
    store.create_project("project-b", display_name="Project B")
    config = dataclasses.replace(
        _hosted_config(issuer),
        hosted_runtime_names=frozenset(),
        hosted_session_statuses=frozenset(),
        hosted_event_names=frozenset(),
    )
    client = TestClient(create_app(store=store, config=config))

    authorized = client.get(
        "/v1/incidents",
        headers={"Authorization": f"Bearer {_token(issuer, project_id='project-a')}"},
    )
    assert authorized.status_code == 200

    mismatched_assertion = client.get(
        "/v1/incidents",
        headers={
            "Authorization": f"Bearer {_token(issuer, project_id='project-a')}",
            "X-Earshot-Project-Id": "project-b",
        },
    )
    assert mismatched_assertion.status_code == 403
    assert mismatched_assertion.json()["error"]["code"] == "EARSHOT_PROJECT_MISMATCH"

    wrong_scope = client.get(
        "/v1/incidents",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id='project-a', scope='earshot:write')}"
            )
        },
    )
    assert wrong_scope.status_code == 403
    assert wrong_scope.json()["error"]["code"] == "EARSHOT_SCOPE_REQUIRED"

    absent = client.get("/v1/incidents")
    assert absent.status_code == 401

    unprovisioned = client.get(
        "/v1/incidents",
        headers={"Authorization": f"Bearer {_token(issuer, project_id='project-c')}"},
    )
    assert unprovisioned.status_code == 403
    assert unprovisioned.json()["error"]["code"] == "EARSHOT_PROJECT_NOT_PROVISIONED"


def test_hosted_capture_rejects_free_text_coverage_and_hmacs_client_ids(tmp_path, issuer) -> None:
    project_id = "hosted-capture"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted capture")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    secret_label = "customer-jane-doe"
    headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }
    body = {
        "captureVersion": 1,
        "sessionId": secret_label,
        "clockDomain": {
            "id": "clock-customer-jane-doe",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "snapshots": [],
        "coverage": [
            {
                "signal": "capture.coverage",
                "availability": "partial",
                "reason": secret_label,
            }
        ],
    }

    rejected = client.post("/v1/capture", json=body, headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"
    assert secret_label not in rejected.text
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0

    body["coverage"] = []
    accepted = client.post("/v1/capture", json=body, headers=headers)
    assert accepted.status_code == 201, accepted.text
    assert secret_label not in accepted.text
    artifact = store.get_artifact(accepted.json()["bundle_id"], project_id=project_id)[1]
    bundle = decode_incident_protobuf(artifact)
    assert bundle.profile.manifest.session_id != secret_label
    assert bundle.profile.manifest.session_id.startswith("sess_")
    assert secret_label.encode() not in artifact
    assert b"clock-customer-jane-doe" not in artifact
    replayed = client.post("/v1/capture", json=body, headers=headers)
    assert replayed.status_code == 200
    assert replayed.json()["bundle_id"] == accepted.json()["bundle_id"]


def test_hosted_capture_resync_reasons_are_finite(tmp_path, issuer) -> None:
    project_id = "hosted-resync"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted resync")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    body = {
        "captureVersion": 2,
        "sessionId": "sess-0123456789abcdef",
        "clockDomain": {
            "id": "clk-0123456789abcdef",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 2,
        "snapshots": [],
        "resync": {
            "missedFromSequence": 1,
            "missedThroughSequence": 1,
            "reason": "customer-password-reset-token",
        },
    }

    response = client.post(
        "/v1/capture",
        json=body,
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_capture_accepts_transport_drop_resync_reason(tmp_path, issuer) -> None:
    project_id = "hosted-upload-resync"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted upload resync")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    client = TestClient(
        create_app(
            store=store,
            config=_hosted_config(issuer),
            capture_journal_dir=capture_dir,
        )
    )
    body = {
        "captureVersion": 2,
        "sessionId": "sess-0123456789abcdef",
        "clockDomain": {
            "id": "clk-0123456789abcdef",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 2,
        "snapshots": [],
        "resync": {
            "missedFromSequence": 1,
            "missedThroughSequence": 1,
            "reason": "upload_failed_payload_dropped",
        },
    }

    response = client.post(
        "/v1/capture",
        json=body,
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            )
        },
    )

    assert response.status_code == 202, response.text


def test_hosted_continuous_capture_requires_durable_journal_storage(tmp_path, issuer) -> None:
    project_id = "hosted-durable-capture"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted durable capture")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    response = client.post(
        "/v1/capture",
        json={
            "captureVersion": 2,
            "sessionId": "sess-0123456789abcdef",
            "clockDomain": {
                "id": "clk-0123456789abcdef",
                "kind": "browser_monotonic",
                "unit": "ms",
                "uncertaintyMs": 1,
                "wallOriginMs": None,
            },
            "drainSequence": 1,
            "snapshots": [],
        },
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            )
        },
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert response.json()["error"]["code"] == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_capture_retry_fails_closed_after_correlation_key_loss(tmp_path, issuer) -> None:
    project_id = "hosted-key-loss"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted key loss")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    body = {
        "captureVersion": 2,
        "sessionId": "session-hosted-key-loss",
        "clockDomain": {
            "id": "clock-hosted-key-loss",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
    }
    headers = {
        "Authorization": (f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}")
    }

    with TestClient(app) as client:
        accepted = client.post("/v1/capture", json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text

    (ledger_path,) = capture_dir.glob(f"*{SIDECAR_SUFFIX}")
    original_ledger = ledger_path.read_bytes()
    store.close()
    (tmp_path / "instance-correlation.key").unlink()

    reopened_store = IncidentStore(tmp_path)
    reopened_app = create_app(
        store=reopened_store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    with TestClient(reopened_app) as client:
        assert client.app.state.capture_calls.durable_available is False
        retry = client.post("/v1/capture", json=body, headers=headers)

    assert retry.status_code == 503
    assert retry.headers["Retry-After"] == "3"
    assert retry.json()["error"]["code"] == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert ledger_path.read_bytes() == original_ledger
    reopened_store.close()


@pytest.mark.parametrize(
    "inventory_damage",
    [
        "orphan_journal",
        "temporary_sidecar",
        "malformed_sidecar",
        "missing_applied_journal",
        "mismatched_correlation_key",
    ],
)
def test_hosted_capture_fails_closed_on_unattributable_journal_inventory(
    tmp_path, issuer, inventory_damage
) -> None:
    project_id = "hosted-inventory-check"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted inventory check")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    call_key = f"call-{inventory_damage}"
    repair_path = None
    if inventory_damage == "orphan_journal":
        repair_path = journal_path(capture_dir, call_key)
        repair_path.write_bytes(b"unattributed journal")
    elif inventory_damage == "temporary_sidecar":
        temporary = sidecar_path(capture_dir, call_key).with_name(
            sidecar_path(capture_dir, call_key).name + ".tmp"
        )
        temporary.write_bytes(b"unfinished sidecar write")
    elif inventory_damage == "malformed_sidecar":
        sidecar_path(capture_dir, call_key).write_bytes(b"{malformed")
    else:
        correlation_key_id = store.fingerprint("earshot.capture-ledger-key-id.v1", "instance")
        if inventory_damage == "mismatched_correlation_key":
            journal_path(capture_dir, call_key).write_bytes(b"owned by an earlier instance key")
            correlation_key_id = "b" * 64
        write_sidecar(
            capture_dir,
            CaptureSidecar(
                project_id=project_id,
                call_key=call_key,
                applied=1,
                digests={1: "a" * 64},
                turn_sequence=0,
                first_observed_ms=None,
                last_observed_ms=None,
                lossy=False,
                finalized=False,
                correlation_key_id=correlation_key_id,
            ),
        )
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    before = {path.name: path.read_bytes() for path in capture_dir.iterdir() if path.is_file()}

    with TestClient(app) as client:
        assert client.app.state.capture_calls.durable_available is False
        response = client.post(
            "/v1/capture",
            json={
                "captureVersion": 2,
                "sessionId": "sess-hosted-inventory",
                "clockDomain": {
                    "id": "clk-hosted-inventory",
                    "kind": "browser_monotonic",
                    "unit": "ms",
                    "uncertaintyMs": 1,
                    "wallOriginMs": None,
                },
                "drainSequence": 1,
                "snapshots": [],
            },
            headers={
                "Authorization": (
                    f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
                )
            },
        )

    after = {path.name: path.read_bytes() for path in capture_dir.iterdir() if path.is_file()}
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert response.json()["error"]["code"] == "EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE"
    assert after == before
    if repair_path is not None:
        repair_path.unlink()
        assert client.app.state.capture_calls.durable_available is True
    if inventory_damage == "mismatched_correlation_key":
        assert client.app.state.capture_calls.drop_project(project_id) is True
        assert client.app.state.capture_calls.durable_available is True
    store.close()


def test_hosted_capture_revalidates_inventory_after_journal_mount_replacement(
    tmp_path, issuer
) -> None:
    project_id = "hosted-replaced-mount"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted replaced mount")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    orphan = journal_path(capture_dir, "call-from-new-volume")
    replacement = tmp_path / "previous-capture-volume"
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-replaced-volume",
        "clockDomain": {
            "id": "clk-hosted-replaced-volume",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
    }
    headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }

    with TestClient(app) as client:
        capture_dir.rename(replacement)
        capture_dir.mkdir()
        orphan.write_bytes(b"unattributed replacement volume journal")
        first_response = client.post("/v1/capture", json=body, headers=headers)
        assert first_response.status_code == 503
        assert client.app.state.capture_calls.durable_available is False
        response = client.post("/v1/capture", json=body, headers=headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert orphan.read_bytes() == b"unattributed replacement volume journal"
    store.close()


def test_hosted_capture_stays_unavailable_when_sealed_journal_cleanup_fails(
    tmp_path, issuer, monkeypatch
) -> None:
    from earshot.capture import calls as capture_calls

    project_id = "hosted-sealed-cleanup-failure"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted sealed cleanup failure")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    call_key = "sealed-call"
    expected_key_id = store.fingerprint("earshot.capture-ledger-key-id.v1", "instance")
    write_sidecar(
        capture_dir,
        CaptureSidecar(
            project_id=project_id,
            call_key=call_key,
            applied=1,
            digests={1: "a" * 64},
            turn_sequence=0,
            first_observed_ms=None,
            last_observed_ms=None,
            lossy=False,
            finalized=True,
            correlation_key_id=expected_key_id,
            sealed=True,
        ),
    )
    journal_path(capture_dir, call_key).write_bytes(b"sealed journal awaiting cleanup")

    def fail_cleanup(_directory, _call_key) -> None:
        raise OSError("simulated sealed journal directory fsync failure")

    monkeypatch.setattr(capture_calls, "remove_sealed_journal", fail_cleanup)
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    with TestClient(app) as client:
        assert client.app.state.capture_calls.durable_available is False
        response = client.post(
            "/v1/capture",
            json={
                "captureVersion": 2,
                "sessionId": "sess-hosted-cleanup-failure",
                "clockDomain": {
                    "id": "clk-hosted-cleanup-failure",
                    "kind": "browser_monotonic",
                    "unit": "ms",
                    "uncertaintyMs": 1,
                    "wallOriginMs": None,
                },
                "drainSequence": 1,
                "snapshots": [],
            },
            headers={
                "Authorization": (
                    f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
                )
            },
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert journal_path(capture_dir, call_key).read_bytes() == b"sealed journal awaiting cleanup"
    store.close()


def test_hosted_unreplayable_journal_stays_fenced_until_project_deletion(tmp_path, issuer) -> None:
    project_id = "hosted-corrupt-journal"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted corrupt journal")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-corrupt-journal",
        "clockDomain": {
            "id": "clk-hosted-corrupt-journal",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
    }
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/capture",
            json=body,
            headers={
                "Authorization": (
                    f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
                )
            },
        )
    assert accepted.status_code == 202, accepted.text

    call_key = accepted.json()["call_id"]
    journal = journal_path(capture_dir, call_key)
    ledger = sidecar_path(capture_dir, call_key)
    corrupt_journal = b"not a replayable checkpoint journal"
    journal.write_bytes(corrupt_journal)
    ledger_before = ledger.read_bytes()

    reopened_app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    with TestClient(reopened_app) as client:
        assert client.app.state.capture_calls.durable_available is False
        retry = client.post(
            "/v1/capture",
            json=body,
            headers={
                "Authorization": (
                    f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
                )
            },
        )
        assert retry.status_code == 503
        assert retry.headers["Retry-After"] == "3"
        assert journal.read_bytes() == corrupt_journal
        assert ledger.read_bytes() == ledger_before

        deletion_token = _token(
            issuer,
            project_id=project_id,
            scope="earshot:project:delete",
        )
        deleted = client.delete(
            f"/v1/projects/{project_id}",
            headers={"Authorization": f"Bearer {deletion_token}"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["state"] == "deleted"
        assert client.app.state.capture_calls.durable_available is True

    assert not journal.exists()
    assert not ledger.exists()
    store.close()


def test_hosted_live_sealing_waits_for_the_accepted_artifact_contract(tmp_path, issuer) -> None:
    project_id = "hosted-seal-pending"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted seal pending")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/live/sessions/runtime-session/seal",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            )
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_CONTRACT_NOT_ACCEPTED"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_finalized_capture_can_be_sealed_to_release_its_durable_slot(
    tmp_path, issuer
) -> None:
    from earshot.live import LiveConfig, LiveSessionRegistry

    project_id = "hosted-capture-seal"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted capture seal")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    live = LiveSessionRegistry(config=LiveConfig(max_sessions=1, max_sessions_per_project=1))
    client = TestClient(
        create_app(
            store=store,
            config=_hosted_config(issuer),
            live_registry=live,
            capture_journal_dir=capture_dir,
        )
    )
    headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-capture-seal",
        "clockDomain": {
            "id": "clk-hosted-capture-seal",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
        "end": {"reason": "call_ended", "timestampMs": 1100},
    }

    accepted = client.post("/v1/capture", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    call_id = accepted.json()["call_id"]
    sealed = client.post(f"/v1/live/sessions/{call_id}/seal", headers=headers)

    assert sealed.status_code == 201, sealed.text
    assert sealed.json()["close_observed"] is True
    assert read_sidecar(sidecar_path(capture_dir, call_id)).sealed is True
    assert not journal_path(capture_dir, call_id).exists()
    next_call = {
        **body,
        "sessionId": "sess-hosted-capture-seal-next",
        "clockDomain": {**body["clockDomain"], "id": "clk-hosted-capture-seal-next"},
        "end": None,
    }
    next_accepted = client.post("/v1/capture", json=next_call, headers=headers)
    assert next_accepted.status_code == 202, next_accepted.text
    client.app.state.live.close()


def test_hosted_seal_retry_cleans_up_after_artifact_exists_but_journal_ack_fails(
    tmp_path, issuer, monkeypatch
) -> None:
    from earshot.capture.calls import CaptureJournalUnavailableError
    from earshot.live import LiveConfig, LiveSessionRegistry

    project_id = "hosted-capture-seal-retry"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted capture seal retry")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        live_registry=LiveSessionRegistry(
            config=LiveConfig(max_sessions=1, max_sessions_per_project=1)
        ),
        capture_journal_dir=capture_dir,
    )
    write_headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }
    read_headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:read')}"
    }
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-capture-seal-retry",
        "clockDomain": {
            "id": "clk-hosted-capture-seal-retry",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
        "end": {"reason": "call_ended", "timestampMs": 1100},
    }

    with TestClient(app) as client:
        accepted = client.post("/v1/capture", json=body, headers=write_headers)
        assert accepted.status_code == 202, accepted.text
        call_id = accepted.json()["call_id"]
        capture_calls = client.app.state.capture_calls
        mark_sealed = capture_calls.mark_sealed

        def fail_mark_sealed(_project_id: str, _call_key: str) -> None:
            raise CaptureJournalUnavailableError("injected seal acknowledgment failure")

        monkeypatch.setattr(capture_calls, "mark_sealed", fail_mark_sealed)
        first_seal = client.post(f"/v1/live/sessions/{call_id}/seal", headers=write_headers)
        assert first_seal.status_code == 503
        assert first_seal.headers["Retry-After"] == "3"

        # Artifact presence is not proof that the durable capture slot was released.
        artifacts = client.get(
            "/v1/incidents", params={"session_id": call_id}, headers=read_headers
        )
        assert artifacts.status_code == 200
        assert [item["finality"] for item in artifacts.json()["items"]] == ["final"]
        live_sessions = client.get("/v1/live/sessions", headers=read_headers)
        assert any(
            item["session_id"] == call_id and item["state"] == "finalized"
            for item in live_sessions.json()["items"]
        )

        monkeypatch.setattr(capture_calls, "mark_sealed", mark_sealed)
        retried_seal = client.post(f"/v1/live/sessions/{call_id}/seal", headers=write_headers)
        assert retried_seal.status_code == 200, retried_seal.text
        remaining_sessions = client.get("/v1/live/sessions", headers=read_headers)
        assert all(item["session_id"] != call_id for item in remaining_sessions.json()["items"])
    client.app.state.capture_calls.close()


def test_project_deletion_waits_for_capture_seal_fence(tmp_path, issuer, monkeypatch) -> None:
    from earshot.capture import calls as capture_calls_module

    project_id = "hosted-seal-delete-race"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted seal deletion race")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        capture_journal_dir=capture_dir,
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-seal-delete-race",
        "clockDomain": {
            "id": "clk-hosted-seal-delete-race",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
        "end": {"reason": "call_ended", "timestampMs": 1100},
    }
    accepted = client.post("/v1/capture", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    call_id = accepted.json()["call_id"]

    write_started = threading.Event()
    resume_write = threading.Event()
    deletion_fence_attempted = threading.Event()
    deletion_cleanup_started = threading.Event()
    original_write_sidecar = capture_calls_module.write_sidecar
    original_begin_project_deletion = store.begin_project_deletion
    original_drop_project = app.state.capture_calls.drop_project

    def pause_sealed_sidecar(directory, sidecar) -> None:
        if sidecar.call_key == call_id and sidecar.sealed:
            write_started.set()
            assert resume_write.wait(timeout=5)
        original_write_sidecar(directory, sidecar)

    def observe_deletion_cleanup(deleting_project_id: str) -> bool:
        deletion_cleanup_started.set()
        return original_drop_project(deleting_project_id)

    def observe_deletion_fence(deleting_project_id: str) -> str:
        deletion_fence_attempted.set()
        return original_begin_project_deletion(deleting_project_id)

    monkeypatch.setattr(capture_calls_module, "write_sidecar", pause_sealed_sidecar)
    monkeypatch.setattr(store, "begin_project_deletion", observe_deletion_fence)
    monkeypatch.setattr(app.state.capture_calls, "drop_project", observe_deletion_cleanup)
    deletion_headers = {
        "Authorization": (
            f"Bearer {_token(issuer, project_id=project_id, scope='earshot:project:delete')}"
        )
    }

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                sealing = executor.submit(
                    client.post,
                    f"/v1/live/sessions/{call_id}/seal",
                    headers=headers,
                )
                assert write_started.wait(timeout=5), (
                    "seal did not reach its durable acknowledgment"
                )
                deletion = executor.submit(
                    client.delete,
                    f"/v1/projects/{project_id}",
                    headers=deletion_headers,
                )
                assert deletion_fence_attempted.wait(timeout=5), (
                    "project deletion did not reach its write fence"
                )
                assert not deletion_cleanup_started.wait(timeout=0.2), (
                    "project deletion removed capture state before the seal fence completed"
                )
                resume_write.set()
                sealed = sealing.result(timeout=5)
                deleted = deletion.result(timeout=5)
            finally:
                resume_write.set()
    finally:
        resume_write.set()

    assert sealed.status_code == 201, sealed.text
    assert deleted.status_code == 200, deleted.text
    assert store.project_lifecycle(project_id) == "deleted"
    assert not sidecar_path(capture_dir, call_id).exists()
    assert not journal_path(capture_dir, call_id).exists()
    client.close()
    store.close()


def test_project_deletion_waits_for_capture_expiry_fence(tmp_path, issuer, monkeypatch) -> None:
    from earshot.capture import calls as capture_calls_module
    from earshot.live import LiveConfig, LiveSessionRegistry

    project_id = "hosted-expiry-delete-race"
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted expiry deletion race")
    capture_dir = tmp_path / "capture-journals"
    capture_dir.mkdir()
    now = [1_000.0]
    live = LiveSessionRegistry(
        config=LiveConfig(
            poll_interval_ms=3_600_000,
            stale_after_seconds=1.0,
            session_ttl_seconds=60.0,
        ),
        clock=lambda: now[0],
    )
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        live_registry=live,
        capture_journal_dir=capture_dir,
    )
    headers = {
        "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
    }
    body = {
        "captureVersion": 2,
        "sessionId": "sess-hosted-expiry-delete-race",
        "clockDomain": {
            "id": "clk-hosted-expiry-delete-race",
            "kind": "browser_monotonic",
            "unit": "ms",
            "uncertaintyMs": 1,
            "wallOriginMs": None,
        },
        "drainSequence": 1,
        "snapshots": [],
    }
    write_started = threading.Event()
    resume_write = threading.Event()
    deletion_fence_attempted = threading.Event()
    deletion_cleanup_started = threading.Event()
    original_write_sidecar = capture_calls_module.write_sidecar
    original_begin_project_deletion = store.begin_project_deletion
    original_drop_project = app.state.capture_calls.drop_project

    def pause_expired_sidecar(directory, sidecar) -> None:
        if sidecar.call_key == call_id and sidecar.expired:
            write_started.set()
            assert resume_write.wait(timeout=5)
        original_write_sidecar(directory, sidecar)

    def observe_deletion_fence(deleting_project_id: str) -> str:
        deletion_fence_attempted.set()
        return original_begin_project_deletion(deleting_project_id)

    def observe_deletion_cleanup(deleting_project_id: str) -> bool:
        deletion_cleanup_started.set()
        return original_drop_project(deleting_project_id)

    deletion_headers = {
        "Authorization": (
            f"Bearer {_token(issuer, project_id=project_id, scope='earshot:project:delete')}"
        )
    }

    with TestClient(app) as client:
        accepted = client.post("/v1/capture", json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        call_id = accepted.json()["call_id"]
        now[0] += 61.0
        monkeypatch.setattr(capture_calls_module, "write_sidecar", pause_expired_sidecar)
        monkeypatch.setattr(store, "begin_project_deletion", observe_deletion_fence)
        monkeypatch.setattr(app.state.capture_calls, "drop_project", observe_deletion_cleanup)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                try:
                    expiry = executor.submit(live.expire)
                    assert write_started.wait(timeout=5), "expiry did not reach its durable marker"
                    deletion = executor.submit(
                        client.delete,
                        f"/v1/projects/{project_id}",
                        headers=deletion_headers,
                    )
                    assert deletion_fence_attempted.wait(timeout=5), (
                        "project deletion did not reach its write fence"
                    )
                    assert not deletion_cleanup_started.wait(timeout=0.2), (
                        "project deletion removed capture state before the expiry marker completed"
                    )
                    resume_write.set()
                    expiry.result(timeout=5)
                    deleted = deletion.result(timeout=5)
                finally:
                    resume_write.set()
        finally:
            resume_write.set()

    assert deleted.status_code == 200, deleted.text
    assert store.project_lifecycle(project_id) == "deleted"
    assert not sidecar_path(capture_dir, call_id).exists()
    assert not journal_path(capture_dir, call_id).exists()
    store.close()


def test_hosted_runtime_session_cannot_claim_browser_capture_namespace(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Reserved hosted session")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    body = _hosted_metadata_body()
    reserved_session_id = "capture-0123456789abcdef0123456789abcdef"
    profile = body["profile"]
    profile["manifest"]["session_id"] = reserved_session_id
    profile["session"]["session_id"] = reserved_session_id
    for event in profile["events"]:
        event["session_id"] = reserved_session_id

    response = client.post(
        "/v1/incidents",
        json=body,
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Idempotency-Key": "reserved-session-idempotency",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_SESSION_ID_RESERVED"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_unknown_key_refresh_does_not_block_verification_with_a_cached_key(
    issuer, monkeypatch
) -> None:
    verifier = HostedJwtVerifier(
        issuer="https://platform.example.test",
        audience="earshot-api",
        jwks_url=issuer["url"],
        jwks_ca_file=issuer["ca_file"],
    )
    # Trigger the refresh path without waiting for its production cooldown.
    monkeypatch.setattr(hosted_auth, "_UNKNOWN_KID_REFRESH_SECONDS", 0)
    valid_token = _token(issuer, project_id="project-a")
    verifier.verify(valid_token)
    unknown_token = _token(
        issuer,
        project_id="project-a",
        kid="untrusted-random-key",
    )
    server = issuer["server"]
    server.block_jwks_refresh = True

    def reject_unknown() -> None:
        with pytest.raises(InvalidHostedToken):
            verifier.verify(unknown_token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        unknown = pool.submit(reject_unknown)
        assert server.refresh_started.wait(timeout=5)
        known = pool.submit(verifier.verify, valid_token)
        try:
            principal = known.result(timeout=5)
        finally:
            server.release_refresh.set()
        unknown.result(timeout=5)

    assert principal.project_id == "project-a"


def test_unrecognized_key_ids_are_rate_limited_before_jwks_fetch(issuer) -> None:
    verifier = HostedJwtVerifier(
        issuer="https://platform.example.test",
        audience="earshot-api",
        jwks_url=issuer["url"],
        jwks_ca_file=issuer["ca_file"],
    )
    verifier._client.cooldown_duration = 0
    verifier.verify(_token(issuer, project_id="project-a"))

    for kid in ("untrusted-key-a", "untrusted-key-b"):
        with pytest.raises(InvalidHostedToken):
            verifier.verify(_token(issuer, project_id="project-a", kid=kid))

    assert issuer["server"].jwks_requests == 1


def test_hosted_jwt_requires_exact_audience_signature_and_short_lifetime(tmp_path, issuer) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("project-a", display_name="Project A")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    for token in (
        _token(issuer, project_id="project-a", aud=["earshot-api", "other-service"]),
        _token(issuer, project_id="project-a", exp=int(time.time()) + 600),
        _token(issuer, project_id="project-a", iss="https://attacker.example.test"),
        jwt.encode(
            {
                "iss": "https://platform.example.test",
                "aud": "earshot-api",
                "sub": "user-123",
                "project_id": "project-a",
                "scope": "earshot:read",
                "iat": int(time.time()),
                "exp": int(time.time()) + 120,
                "jti": "wrong-signature",
            },
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
            algorithm="RS256",
            headers={"kid": "issuer-key-1"},
        ),
    ):
        response = client.get("/v1/incidents", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "EARSHOT_UNAUTHORIZED"


def test_project_summary_is_host_neutral_and_requires_its_summary_scope(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted project")
    bundle = make_valid_bundle(bundle_id="summary-bundle")
    store.ingest(bundle, project_id=project_id)
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.get(
        f"/v1/projects/{project_id}/summary",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:summary:read')}"
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == project_id
    assert payload["items"][0]["session_id"] == bundle.profile.session.session_id
    assert set(payload["items"][0]) == {
        "session_id",
        "status",
        "framework",
        "framework_truncated",
        "created_at_unix_nano",
    }
    assert SECRET_SENTINEL not in response.text
    insufficient = client.get(
        f"/v1/projects/{project_id}/summary",
        headers={
            "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope='earshot:read')}"
        },
    )
    assert insufficient.status_code == 403
    assert insufficient.json()["error"]["code"] == "EARSHOT_SCOPE_REQUIRED"


def test_hosted_ingest_assigns_earshot_bundle_id_and_is_idempotent(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted ingest project")
    body = _hosted_metadata_body()
    payload = json.dumps(body).encode()
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    headers = {
        "Authorization": (f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"),
        "Idempotency-Key": "event_01J-hosted-artifact",
        "Content-Type": "application/json",
    }

    missing_key = client.post(
        "/v1/incidents",
        content=payload,
        headers={
            "Authorization": headers["Authorization"],
            "Content-Type": "application/json",
        },
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "EARSHOT_IDEMPOTENCY_KEY_REQUIRED"

    producer_id = json.loads(payload)
    producer_id["profile"]["manifest"]["bundle_id"] = "producer-placeholder-id"
    supplied_id = client.post(
        "/v1/incidents",
        content=json.dumps(producer_id).encode(),
        headers=headers,
    )
    assert supplied_id.status_code == 400
    assert supplied_id.json()["error"]["code"] == "EARSHOT_HOSTED_BUNDLE_ID_FORBIDDEN"

    accepted = client.post("/v1/incidents", content=payload, headers=headers)
    assert accepted.status_code == 201, accepted.text
    artifact_id = accepted.json()["bundle_id"]
    assert artifact_id.startswith("bundle-")
    assert accepted.json()["created"] is True
    stored_payload = store.get_artifact(artifact_id, project_id=project_id)[1]
    stored_bundle = decode_incident_protobuf(stored_payload)
    assert stored_bundle.profile.attributes == {"session.id": "tvic-session-01J"}
    assert stored_bundle.profile.manifest.producer.language == "unknown"

    store.close()
    store = IncidentStore(tmp_path)
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    retried = client.post("/v1/incidents", content=payload, headers=headers)
    assert retried.status_code == 200
    assert retried.json()["bundle_id"] == artifact_id
    assert retried.json()["created"] is False

    changed = json.loads(payload)
    created_at = int(changed["profile"]["manifest"]["created_at_unix_nano"])
    changed["profile"]["manifest"]["created_at_unix_nano"] = str(created_at + 1)
    diverged = client.post(
        "/v1/incidents",
        content=json.dumps(changed).encode(),
        headers=headers,
    )
    assert diverged.status_code == 409, diverged.text
    assert diverged.json()["error"]["code"] == "EARSHOT_INCIDENT_CONFLICT"

    openapi = client.get("/openapi.json").json()
    hosted_manifest = openapi["components"]["schemas"]["HostedBundleManifest"]
    assert "bundle_id" not in hosted_manifest["properties"]
    hosted_time = openapi["components"]["schemas"]["HostedTimePoint"]
    assert set(hosted_time["properties"]) == {
        "source_time_unix_nano",
        "observed_time_unix_nano",
    }
    hosted_profile = openapi["components"]["schemas"]["HostedIncidentProfile"]
    assert hosted_profile["additionalProperties"] is False
    assert set(hosted_profile["properties"]) == {
        "manifest",
        "session",
        "events",
        "runtime_session_id",
    }
    ingest_operation = openapi["paths"]["/v1/incidents"]["post"]
    body_schema = ingest_operation["requestBody"]["content"]["application/json"]["schema"]
    assert {entry["$ref"] for entry in body_schema["oneOf"]} == {
        "#/components/schemas/IncidentBundleJson",
        "#/components/schemas/HostedIncidentBundleJson",
    }
    assert "application/x-protobuf" not in ingest_operation["requestBody"]["content"]
    assert (
        "application/vnd.earshot.incident+protobuf"
        not in ingest_operation["requestBody"]["content"]
    )
    idempotency_parameter = next(
        parameter
        for parameter in ingest_operation["parameters"]
        if parameter["name"] == "Idempotency-Key"
    )
    assert idempotency_parameter["schema"]["maxLength"] == 128


def test_hosted_ingest_rejects_raw_payloads_and_unapproved_provider_labels(
    tmp_path, issuer
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    headers = {
        "Authorization": (f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"),
        "Content-Type": "application/json",
    }

    raw_body = _hosted_metadata_body(include_raw_otlp=True)
    raw_response = client.post(
        "/v1/incidents",
        content=json.dumps(raw_body).encode(),
        headers={**headers, "Idempotency-Key": "raw-payload-key"},
    )
    assert raw_response.status_code == 422
    assert raw_response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"

    provider_body = _hosted_metadata_body()
    provider_body["profile"]["events"][0]["attributes"] = {
        "gen_ai.request.model": "unapproved-model"
    }
    provider_response = client.post(
        "/v1/incidents",
        content=json.dumps(provider_body).encode(),
        headers={**headers, "Idempotency-Key": "provider-label-key"},
    )
    assert provider_response.status_code == 422
    assert provider_response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "openai/gpt-4o"), ("version", "latest")],
)
def test_hosted_runtime_identity_requires_a_bounded_runtime_slug_and_release(
    tmp_path, issuer, field: str, value: str
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    body = _hosted_metadata_body()
    body["profile"]["manifest"]["producer"][field] = value
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": f"invalid-runtime-{field}",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "customer.phone.number"),
        ("event_name", "customer.email.address"),
        ("runtime_name", "provider.openai"),
    ],
)
def test_hosted_ingest_rejects_unapproved_bounded_codes(
    tmp_path, issuer, field: str, value: str
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    body = _hosted_metadata_body()
    if field == "status":
        body["profile"]["session"]["status"] = value
    elif field == "event_name":
        body["profile"]["events"][0]["event_name"] = value
    else:
        body["profile"]["manifest"]["producer"]["name"] = value
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": f"unapproved-code-{field}",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_EVENT_UNSUPPORTED"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_ingest_stays_closed_until_event_contract_allowlists_are_configured(
    tmp_path, issuer
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Unconfigured hosted contract")
    config = dataclasses.replace(
        _hosted_config(issuer),
        hosted_runtime_names=frozenset(),
        hosted_session_statuses=frozenset(),
        hosted_event_names=frozenset(),
    )
    client = TestClient(create_app(store=store, config=config))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(_hosted_metadata_body()).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": "unconfigured-event-contract",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_CONTRACT_NOT_ACCEPTED"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_runtime_version_rejects_freeform_prerelease_and_build_labels(
    tmp_path, issuer
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Strict runtime version")
    body = _hosted_metadata_body()
    body["profile"]["manifest"]["producer"]["version"] = "1.2.3-provider-openai+customer-model"
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": "unapproved-runtime-version",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_checkpoint_writes_wait_for_the_accepted_runtime_contract(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Hosted checkpoint project")
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/live/sessions/runtime-session-01/checkpoints",
        content=b"untrusted runtime journal bytes",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/vnd.earshot.checkpoint+frames",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_CONTRACT_NOT_ACCEPTED"
    assert (
        client.get(
            "/v1/live/sessions",
            headers={"Authorization": f"Bearer {_token(issuer, project_id=project_id)}"},
        ).json()["items"]
        == []
    )
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_hosted_time_rejects_monotonic_values_without_an_approved_clock_contract(
    tmp_path, issuer
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    body = _hosted_metadata_body()
    body["profile"]["events"][0]["time"].update(
        {"monotonic_time_nano": "4200000000", "clock_domain_id": "runtime-session"}
    )
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": "unapproved-monotonic-clock",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"


@pytest.mark.parametrize("capture_class", ["transcript", "audio", "tool_payload"])
def test_hosted_ingest_rejects_explicit_nonmetadata_capture_policy(
    tmp_path, issuer, capture_class: str
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    body = _hosted_metadata_body()
    body["profile"]["privacy"] = {
        "capture_classes": [{"capture_class": capture_class, "decision": "allow", "captured": True}]
    }
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": f"nonmetadata-{capture_class}",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"


@pytest.mark.parametrize(
    ("location", "sentinel"),
    [
        ("event_attributes", "TRANSCRIPT_SENTINEL_DO_NOT_STORE"),
        ("operation_attributes", "TOOL_PAYLOAD_SENTINEL_DO_NOT_STORE"),
        ("profile_attributes", "ARBITRARY_RUNTIME_DUMP_SENTINEL_DO_NOT_STORE"),
    ],
)
def test_hosted_ingest_rejects_unapproved_content_before_persisting(
    tmp_path, issuer, location: str, sentinel: str
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Metadata-only hosted project")
    body = _hosted_metadata_body()
    if location == "event_attributes":
        body["profile"]["events"][0]["attributes"] = {"service.name": sentinel}
    elif location == "operation_attributes":
        body["profile"]["operations"] = [{"attributes": {"service.name": sentinel}}]
    else:
        body["profile"]["attributes"] = {"service.name": sentinel}
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))

    response = client.post(
        "/v1/incidents",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:write')}"
            ),
            "Content-Type": "application/json",
            "Idempotency-Key": f"private-content-{location}",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_METADATA_ONLY"
    assert sentinel not in response.text
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_platform_summary_adapter_is_opt_in_and_uses_signed_project_id(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Platform project")
    bundle = make_valid_bundle(bundle_id="platform-summary-bundle")
    store.ingest(bundle, project_id=project_id)
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        enable_platform_adapter=True,
    )
    client = TestClient(app)
    headers = {
        "Authorization": (
            f"Bearer {_token(issuer, project_id=project_id, scope='earshot:summary:read')}"
        )
    }

    response = client.get(
        f"/v1/platform/projects/{project_id}/observe/summary",
        headers=headers,
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == bundle.profile.session.session_id
    assert item["href"].startswith("/observe?")
    assert SECRET_SENTINEL not in response.text

    mismatch = client.get(
        f"/v1/platform/projects/{project_id}/observe/summary",
        headers={**headers, "x-platform-project-id": str(uuid.uuid4())},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == "EARSHOT_PROJECT_MISMATCH"

    without_adapter = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    absent = without_adapter.get(
        f"/v1/platform/projects/{project_id}/observe/summary",
        headers=headers,
    )
    assert absent.status_code == 404


def test_hosted_openapi_publishes_scope_requirements_and_hides_operator_auth(
    tmp_path, issuer
) -> None:
    client = TestClient(create_app(store=IncidentStore(tmp_path), config=_hosted_config(issuer)))
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/v1/incidents"]["get"]["x-required-scope"] == "earshot:read"
    assert schema["paths"]["/v1/incidents"]["post"]["x-required-scope"] == "earshot:write"
    assert (
        schema["paths"]["/v1/projects/{project_id}/summary"]["get"]["x-required-scope"]
        == "earshot:summary:read"
    )
    assert (
        schema["paths"]["/v1/incidents/{bundle_id}"]["delete"]["x-required-scope"]
        == "earshot:artifact:delete"
    )
    assert "HostedServiceBearer" in schema["components"]["securitySchemes"]
    assert "BrowserSession" not in schema["components"]["securitySchemes"] or all(
        "BrowserSession" not in security
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
        for security in operation.get("security", [])
    )
    for path, item in schema["paths"].items():
        if not path.startswith("/v1/") or path.startswith("/v1/auth/"):
            continue
        for operation in item.values():
            if isinstance(operation, dict) and "responses" in operation:
                assert operation.get("x-required-scope")


def test_project_deletion_requires_scope_fences_requests_and_retries_pending_cleanup(
    tmp_path, issuer, monkeypatch
) -> None:
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_a, display_name="Project A")
    store.create_project(project_b, display_name="Project B")
    app = create_app(store=store, config=_hosted_config(issuer))
    client = TestClient(app)

    insufficient = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:delete')}"
            )
        },
    )
    assert insufficient.status_code == 403
    assert store.project_lifecycle(project_a) == "active"

    mismatch = client.delete(
        f"/v1/projects/{project_b}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert mismatch.status_code == 403
    assert store.project_lifecycle(project_b) == "active"

    monkeypatch.setattr(app.state.capture_calls, "drop_project", lambda _project_id: False)
    pending = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert pending.status_code == 202
    assert pending.headers["Retry-After"] == "3"
    assert pending.json() == {
        "project_id": project_a,
        "state": "deleting",
        "retry_after_seconds": 3,
    }
    denied_while_deleting = client.get(
        "/v1/incidents",
        headers={"Authorization": f"Bearer {_token(issuer, project_id=project_a)}"},
    )
    assert denied_while_deleting.status_code == 410

    monkeypatch.setattr(app.state.capture_calls, "drop_project", lambda _project_id: True)

    def report_busy_scrub() -> None:
        raise StorageCleanupPendingError("SQLite WAL is busy; cleanup requires retry")

    monkeypatch.setattr(store, "_scrub_deleted_pages", report_busy_scrub)
    storage_pending = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert storage_pending.status_code == 202
    assert storage_pending.headers["Retry-After"] == "3"
    assert storage_pending.json()["state"] == "deleting"

    monkeypatch.setattr(store, "_scrub_deleted_pages", lambda: None)
    completed = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "deleted"
    repeated = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["state"] == "deleted"

    # A completed deletion is terminal even if the external capture volume is
    # later unavailable; a retry must not regress the durable lifecycle response.
    monkeypatch.setattr(app.state.capture_calls, "drop_project", lambda _project_id: False)
    repeated_after_volume_loss = client.delete(
        f"/v1/projects/{project_a}",
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_a, scope='earshot:project:delete')}"
            )
        },
    )
    assert repeated_after_volume_loss.status_code == 200
    assert repeated_after_volume_loss.json()["state"] == "deleted"

    other_project = client.get(
        "/v1/incidents",
        headers={"Authorization": f"Bearer {_token(issuer, project_id=project_b)}"},
    )
    assert other_project.status_code == 200


def test_project_deletion_revokes_live_tail_before_capture_cleanup(
    tmp_path, issuer, monkeypatch
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path / "data")
    store.create_project(project_id, display_name="Live tail deletion")
    journal_dir = tmp_path / "journals"
    writer = CheckpointWriter(CheckpointConfig(checkpoint_dir=journal_dir))
    recorder = IncidentRecorder(
        session_id="session-before-delete",
        bundle_id="bundle-before-delete",
        checkpoint=writer,
    )
    recorder.record_event("earshot.turn.start", turn_id="turn-1")
    journal = next(journal_dir.glob("*.eck"))
    live = LiveSessionRegistry(config=LiveConfig(poll_interval_ms=3_600_000))
    live.accept_frames("session-before-delete", journal.read_bytes(), project_id=project_id)
    subscription = live.subscribe("session-before-delete", project_id=project_id)
    app = create_app(store=store, config=_hosted_config(issuer), live_registry=live)

    def inspect_revoked_tail(deleting_project_id: str) -> bool:
        assert deleting_project_id == project_id
        assert subscription.drain() == []
        ended = subscription.terminal()
        assert [event.name for event in ended] == [EVENT_END]
        assert json.loads(ended[0].payload)["reason"] == END_PROJECT_DELETED
        return True

    monkeypatch.setattr(app.state.capture_calls, "drop_project", inspect_revoked_tail)
    headers = {
        "Authorization": (
            f"Bearer {_token(issuer, project_id=project_id, scope='earshot:project:delete')}"
        )
    }

    try:
        with TestClient(app) as client:
            response = client.delete(f"/v1/projects/{project_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "deleted"
    finally:
        writer.release()
        store.close()


def test_project_deletion_defers_global_compaction_until_capture_cleanup_completes(
    tmp_path, issuer, monkeypatch
) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Deferred compaction")
    app = create_app(store=store, config=_hosted_config(issuer))
    client = TestClient(app)
    compactions = 0

    def compact() -> None:
        nonlocal compactions
        compactions += 1

    monkeypatch.setattr(store, "_scrub_deleted_pages", compact)
    monkeypatch.setattr(app.state.capture_calls, "drop_project", lambda _project_id: False)
    headers = {
        "Authorization": (
            f"Bearer {_token(issuer, project_id=project_id, scope='earshot:project:delete')}"
        )
    }

    assert client.delete(f"/v1/projects/{project_id}", headers=headers).status_code == 202
    assert client.delete(f"/v1/projects/{project_id}", headers=headers).status_code == 202
    assert compactions == 0

    monkeypatch.setattr(app.state.capture_calls, "drop_project", lambda _project_id: True)
    completed = client.delete(f"/v1/projects/{project_id}", headers=headers)
    assert completed.status_code == 200
    assert compactions == 1


def test_artifact_deletion_requires_its_own_hosted_scope(tmp_path, issuer) -> None:
    project_id = str(uuid.uuid4())
    store = IncidentStore(tmp_path)
    store.create_project(project_id, display_name="Artifact deletion project")
    bundle = make_valid_bundle(bundle_id="scope-protected-artifact")
    store.ingest(bundle, project_id=project_id)
    client = TestClient(create_app(store=store, config=_hosted_config(issuer)))
    path = f"/v1/incidents/{bundle.profile.manifest.bundle_id}"

    reference_scope = client.delete(
        path,
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:delete')}"
            )
        },
    )
    assert reference_scope.status_code == 403
    assert reference_scope.json()["error"]["code"] == "EARSHOT_SCOPE_REQUIRED"
    assert (
        client.get(
            path,
            headers={"Authorization": f"Bearer {_token(issuer, project_id=project_id)}"},
        ).status_code
        == 200
    )

    artifact_scope = client.delete(
        path,
        headers={
            "Authorization": (
                f"Bearer {_token(issuer, project_id=project_id, scope='earshot:artifact:delete')}"
            )
        },
    )
    assert artifact_scope.status_code == 204
