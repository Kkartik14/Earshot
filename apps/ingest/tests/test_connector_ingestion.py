"""One delivery, one mutation: what a connector writes, where, and what it refuses.

- **Recording identity:** a provider recording that exists in any state gains no
  row and no fetch; a purged call's delivery is answered ``purged`` and keeps
  nothing; ``metadata_only`` keeps only the spans; a call ID that is no session key
  is refused (422) before anything is written.
- **Session keys:** a webhook connector keys ``"<provider>:<id>"`` unless it merges,
  and a merging connector's evidence joins the host's call for the same ID.
- **Upgrades:** a connector created before this release accepts deliveries, and a
  delivery 0.3.x applied is a replay when the provider retries it after the upgrade.
- **The hook route:** its answer, and the order of its refusals.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

import test_elevenlabs_connector as elevenlabs
import test_retell_connector as retell
import test_vapi_connector as vapi
from connector_support import (
    SECRET_REFS,
    SECRETS,
    START,
    START_SECONDS,
    body_of,
    connector_world,
    signed_headers,
)
from earshot.api import ApiConfig, create_app
from earshot.blobstore import put_stream
from earshot.connectors import (
    DeliveryIdentifierError,
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore, ProjectInactiveError
from earshot.storage_call_purge import CallPurger
from earshot.storage_calls import CallStore
from earshot.storage_deliveries import DeliveryDigests
from otlp_support import keyed_request, request, serve
from store_support import catalog, catalog_rows, drop_connector_objects, make_store
from upgrade_fixtures import copy_upgrade_fixture, upgrade_fixture_names

pytestmark = pytest.mark.integration


# --- Recording identity and the purged, metadata_only and invalid-ID branches -----------------


@pytest.mark.parametrize("state", ["pending", "stored", "failed"])
def test_a_recording_in_any_state_gains_no_row_and_no_fetch(tmp_path, state) -> None:
    world = connector_world(tmp_path, "retell")
    world.deliver(retell._call("call_ended"))
    [(recording_id,)] = world.rows("SELECT recording_id FROM recordings")
    if state == "stored":
        attempt = world.recordings.begin_upload("support", recording_id)
        sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [b"RIFF" * 64])
        world.recordings.finish_upload(
            "support", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
        world.recordings.end_upload("support", attempt.blob_key)
    elif state == "failed":
        with catalog(tmp_path) as connection:
            connection.execute("UPDATE recordings SET state = 'failed'")
            connection.execute("UPDATE recording_fetches SET state = 'failed', url = NULL")

    analyzed = world.deliver(retell._call("call_analyzed"))

    assert analyzed.disposition == "applied"
    assert world.rows("SELECT recording_id, state FROM recordings") == [(recording_id, state)]
    assert world.rows("SELECT count(*) FROM recording_fetches") == [(1,)]
    assert world.counters()["span_conflicts"] == 0


def test_a_purged_calls_delivery_is_answered_purged_and_keeps_nothing(tmp_path) -> None:
    world = connector_world(tmp_path, "retell")
    ended = world.deliver(retell._call("call_ended"))
    CallPurger(world.store, calls=world.calls, recordings=world.recordings).purge_call(
        "support", ended.call_id
    )

    analyzed = world.deliver(retell._call("call_analyzed"))
    again = world.deliver(retell._call("call_analyzed"))

    assert (analyzed.disposition, analyzed.call_id) == ("purged", None)
    assert (again.disposition, again.receipt_id, again.call_id) == (
        "replayed",
        analyzed.receipt_id,
        None,
    )
    assert world.rows("SELECT count(*) FROM spans") == [(0,)]
    assert world.rows("SELECT count(*) FROM recordings") == [(0,)]
    assert world.rows(
        "SELECT state, call_id FROM delivery_receipts WHERE receipt_id = ?",
        (analyzed.receipt_id,),
    ) == [("applied", None)]
    assert world.counters()["purged_call_evidence_dropped"] > 0


def test_metadata_only_keeps_the_spans_and_no_text_key_or_recording(tmp_path) -> None:
    world = connector_world(tmp_path, "vapi", preset="metadata_only")

    outcome = world.deliver(vapi._report())

    assert outcome.disposition == "applied"
    assert world.rows("SELECT count(*) FROM recordings") == [(0,)]
    assert world.rows("SELECT count(*) FROM recording_fetches") == [(0,)]
    assert world.rows("SELECT session_key FROM calls") == [(None,)]
    stored = b"".join(
        bytes(row[0]) + (row[1] or "").encode()
        for row in world.rows("SELECT payload, text_json FROM spans")
    )
    for secret in (b"Green Valley", b"move my visit", vapi.CALL_ID.encode(), b"CANARY"):
        assert secret not in stored
    assert world.rows("SELECT count(*) FROM spans")[0][0] > 0


@pytest.mark.parametrize(
    "call_id",
    ["x" * 300, " padded-id ", "y" * 254],
    ids=["too-long", "padded", "too-long-once-namespaced"],
)
def test_a_call_id_that_is_no_session_key_is_refused_before_the_claim(tmp_path, call_id) -> None:
    world = connector_world(tmp_path, "vapi")
    report = vapi._report()
    report["message"]["call"]["id"] = call_id

    with pytest.raises(DeliveryIdentifierError):
        world.deliver(report)

    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(0,)]
    assert world.rows("SELECT count(*) FROM spans") == [(0,)]


def test_a_delivery_whose_project_stops_after_the_claim_writes_nothing(tmp_path) -> None:
    world = connector_world(tmp_path, "vapi")
    deliveries = world.ingestion.deliveries
    connector = world.store.get_connector(world.endpoint_id)
    claim = deliveries.claim(
        connector,
        delivery_key_hmac=world.store.fingerprint("delivery:test", "stopping"),
        digests=DeliveryDigests("1" * 64, "2" * 64),
        event_type="end-of-call-report",
        now_unix_nano=START,
    )
    world.store.begin_project_deletion("support")

    with pytest.raises(ProjectInactiveError):
        deliveries.apply(
            connector,
            claim.receipt_id,
            lease_token=claim.lease_token,
            session_key="vapi:stopping",
            spans=[],
            recordings=[],
        )
    assert world.rows("SELECT state FROM delivery_receipts") == [("processing",)]


# --- Session keys --------------------------------------------------------------------------


def test_a_webhook_connector_never_writes_into_a_hosts_call_unless_it_merges(tmp_path) -> None:
    separate = connector_world(tmp_path / "separate", "vapi")
    merged = connector_world(tmp_path / "merged", "vapi", config={"merge_session_keys": True})
    for world in (separate, merged):
        # The host's own bot exported a span keyed by the provider's call ID.
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        from earshot.call_capture import spans_from_request

        host = ExportTraceServiceRequest.FromString(keyed_request(vapi.CALL_ID, 7, 1))
        world.calls.record_spans("support", spans_from_request(host))

    separate_call = separate.deliver(vapi._report()).call_id
    merged_call = merged.deliver(vapi._report()).call_id

    assert separate_call == separate.calls.resolve_call("support", f"vapi:{vapi.CALL_ID}")
    assert separate_call != separate.calls.resolve_call("support", vapi.CALL_ID)
    assert merged_call == merged.calls.resolve_call("support", vapi.CALL_ID)
    assert merged.rows("SELECT count(*) FROM calls") == [(1,)]


# --- Upgrades ---------------------------------------------------------------------------------


@pytest.mark.sqlite_only
@pytest.mark.parametrize("name", [upgrade_fixture_names()[0], upgrade_fixture_names()[-1]])
def test_a_connector_from_a_0_1_catalog_accepts_deliveries_after_the_upgrade(tmp_path, name):
    store = IncidentStore(copy_upgrade_fixture(name, tmp_path / name))
    try:
        connector = store.get_connector("upgrade_fixture_connector")
        assert connector.normalizer_version != "0.2.0"  # created by an older binary
        ingestion = HostedProviderIngestion(
            store,
            secrets=MappingSecretResolver({connector.secret_ref: "upgrade-secret"}),
            now_unix_seconds=lambda: START_SECONDS,
        )
        raw = body_of(vapi._report())

        outcome = ingestion.receive(
            RawProviderDelivery(
                connector.endpoint_id, ((b"authorization", b"Bearer upgrade-secret"),), raw
            )
        )

        assert outcome.disposition == "applied"
        assert outcome.call_id == CallStore(store).resolve_call(
            connector.project_id, f"vapi:{vapi.CALL_ID}"
        )
    finally:
        store.close()


def test_a_delivery_0_3_applied_is_a_replay_after_the_upgrade(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")
    connector = store.create_connector(
        "support",
        provider="vapi",
        secret_ref=SECRET_REFS["vapi"],
        endpoint_id="vapi_connector_0001",
        normalizer_version="0.1.0",
    )
    raw = body_of(vapi._report())
    delivery_key = store.fingerprint(
        f"delivery:{connector.endpoint_id}", f"end-of-call-report:{vapi.CALL_ID}"
    )
    store.close()
    with catalog(tmp_path) as connection:  # what 0.3.x left: schema 0005 and one receipt
        drop_connector_objects(connection)
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0005_projections",))
        connection.execute(
            "INSERT INTO delivery_receipts(receipt_id, project_id, endpoint_id, "
            "delivery_key_hmac, body_sha256, event_type, state, first_received_at_unix_nano, "
            "completed_at_unix_nano, attempt_count) VALUES ('receipt_from_0_3', 'support', ?, "
            "?, ?, 'end-of-call-report', 'applied', 1, 2, 1)",
            (connector.endpoint_id, delivery_key, hashlib.sha256(raw).hexdigest()),
        )
    store = make_store(tmp_path)
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({SECRET_REFS["vapi"]: SECRETS["vapi"]}),
        now_unix_seconds=lambda: START_SECONDS,
    )

    retry = ingestion.receive(
        RawProviderDelivery(connector.endpoint_id, signed_headers("vapi", raw), raw)
    )
    other = vapi._report()
    other["message"]["call"]["id"] = "7a2d3c0f-1e8b-4d66-8b62-4f1a9d3e2b88"
    new = ingestion.receive(
        RawProviderDelivery(
            connector.endpoint_id, signed_headers("vapi", body_of(other)), body_of(other)
        )
    )

    assert (retry.disposition, retry.receipt_id, retry.call_id) == (
        "replayed",
        "receipt_from_0_3",
        None,
    )
    assert new.disposition == "applied"
    assert catalog_rows(tmp_path, "SELECT count(*) FROM delivery_receipts") == [(2,)]


# --- The hook route ----------------------------------------------------------------------------


def _app(tmp_path, provider: str, monkeypatch, **config):
    monkeypatch.setenv(SECRET_REFS[provider].removeprefix("env:"), SECRETS[provider])
    store = make_store(tmp_path)
    connector = store.create_connector(
        "default",
        provider=provider,
        secret_ref=SECRET_REFS[provider],
        endpoint_id="hook_0001_endpoint",
    )
    return create_app(store=store, config=ApiConfig(**config)), connector.endpoint_id


def _post(port, endpoint_id, provider, payload, *, signed=True, body=None, headers=None):
    raw = body_of(payload) if body is None else body
    sent = {"Content-Type": "application/json"}
    if signed:  # signed now: a real server reads the real clock
        signature = signed_headers(provider, raw, at_seconds=int(time.time()))
        sent.update({key.decode(): value.decode() for key, value in signature})
    sent.update(headers or {})
    return request(port, "POST", f"/hooks/v1/connectors/{endpoint_id}", raw, headers=sent)


def test_the_hook_answers_receipt_disposition_and_call(tmp_path, monkeypatch) -> None:
    app, endpoint_id = _app(tmp_path, "retell", monkeypatch)

    with serve(app) as port:
        applied = _post(port, endpoint_id, "retell", retell._call("call_ended"))
        ignored = _post(port, endpoint_id, "retell", {"event": "call_started", "call": {}})

    assert applied.status == 200
    assert set(applied.json()) == {"receipt_id", "disposition", "call_id"}
    assert applied.json()["disposition"] == "applied"
    assert applied.json()["call_id"].startswith("call_")
    assert applied.headers["cache-control"] == "no-store"
    assert ignored.json() == {"receipt_id": None, "disposition": "ignored", "call_id": None}


def test_the_hook_refuses_an_invalid_call_id_with_422(tmp_path, monkeypatch) -> None:
    app, endpoint_id = _app(tmp_path, "vapi", monkeypatch)
    report = vapi._report()
    report["message"]["call"]["id"] = "z" * 300

    with serve(app) as port:
        reply = _post(port, endpoint_id, "vapi", report)

    assert (reply.status, reply.problem_code()) == (422, "EARSHOT_CONNECTOR_ID_INVALID")


def test_the_hook_authenticates_before_it_ignores_or_measures(tmp_path, monkeypatch) -> None:
    app, endpoint_id = _app(tmp_path, "elevenlabs", monkeypatch, max_connector_body_bytes=4096)
    audio = {
        "type": "post_call_audio",
        "event_timestamp": START_SECONDS,
        "data": {"agent_id": "agent_x", "conversation_id": "conv_x", "full_audio": "SUQz" * 4000},
    }
    small_audio = {**audio, "data": {**audio["data"], "full_audio": "SUQz"}}
    transcription = elevenlabs._transcription()

    with serve(app) as port:
        unsigned_small = _post(port, endpoint_id, "elevenlabs", small_audio, signed=False)
        unsigned_large = _post(port, endpoint_id, "elevenlabs", audio, signed=False)
        large_audio = _post(port, endpoint_id, "elevenlabs", audio)
        large_transcription = _post(port, endpoint_id, "elevenlabs", transcription)
        # A well-formed signature, so only the declared length can refuse it.
        signature = dict(
            (key.decode(), value.decode())
            for key, value in signed_headers("elevenlabs", b"{}", at_seconds=int(time.time()))
        )
        beyond = request(
            port,
            "POST",
            f"/hooks/v1/connectors/{endpoint_id}",
            None,
            headers={**signature, "Content-Length": str(65 * 1024 * 1024)},
        )
        unknown = _post(port, "hook_unknown_endpoint", "elevenlabs", small_audio)

    assert (unsigned_small.status, unsigned_small.problem_code()) == (
        401,
        "EARSHOT_CONNECTOR_AUTH_FAILED",
    )
    assert unsigned_large.status == 401
    assert large_audio.json() == {"receipt_id": None, "disposition": "ignored", "call_id": None}
    assert (large_transcription.status, large_transcription.problem_code()) == (
        413,
        "EARSHOT_CONNECTOR_BODY_TOO_LARGE",
    )
    assert beyond.status == 413
    assert unknown.status == 404


def test_a_header_secret_is_checked_before_the_body_type_or_size(tmp_path, monkeypatch) -> None:
    app, endpoint_id = _app(tmp_path, "vapi", monkeypatch, max_connector_body_bytes=1024)
    oversized = b"{" + b" " * 4096 + b"}"

    with serve(app) as port:
        wrong = _post(
            port,
            endpoint_id,
            "vapi",
            None,
            body=oversized,
            signed=False,
            headers={"Authorization": "Bearer wrong"},
        )
        right = _post(port, endpoint_id, "vapi", None, body=oversized)
        text = _post(
            port, endpoint_id, "vapi", vapi._report(), headers={"Content-Type": "text/plain"}
        )

    assert (wrong.status, right.status, text.status) == (401, 413, 415)
    assert json.loads(right.body)["error"]["code"] == "EARSHOT_CONNECTOR_BODY_TOO_LARGE"
