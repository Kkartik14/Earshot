from __future__ import annotations

from dataclasses import replace

import pytest

from earshot.codec import encode_incident_protobuf
from earshot.storage import (
    DeliveryInProgressError,
    IncidentStore,
    StorageError,
)
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.integration


def _connector(store: IncidentStore):
    store.create_project("receipts", display_name="Receipts")
    return store.create_connector(
        "receipts",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_SECRET",
        endpoint_id="receipt_connector_0001",
    )


def test_expired_receipt_lease_reclaims_with_cas_ownership(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    connector = _connector(store)
    delivery_key = store.fingerprint("delivery:test", "delivery-1")
    first = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_key,
        body_sha256="a" * 64,
        event_type="finalized",
        now_unix_nano=1_000_000_000,
        lease_nano=30_000_000_000,
    )
    assert first.lease_token == 1

    with pytest.raises(DeliveryInProgressError) as active:
        store.claim_delivery(
            connector,
            delivery_key_hmac=delivery_key,
            body_sha256="a" * 64,
            event_type="finalized",
            now_unix_nano=2_000_000_000,
            lease_nano=30_000_000_000,
        )
    assert active.value.retry_after_seconds == 29

    reclaimed = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_key,
        body_sha256="a" * 64,
        event_type="finalized",
        now_unix_nano=31_000_000_000,
        lease_nano=30_000_000_000,
    )
    assert reclaimed.receipt_id == first.receipt_id
    assert reclaimed.lease_token == 2

    with pytest.raises(StorageError):
        store.complete_delivery(
            first.receipt_id,
            state="ignored",
            completed_at_unix_nano=32_000_000_000,
            lease_token=first.lease_token or 0,
        )
    with pytest.raises(StorageError):
        store.fail_delivery(
            first.receipt_id,
            lease_token=first.lease_token or 0,
            failure_code="STALE_WORKER",
        )

    store.complete_delivery(
        reclaimed.receipt_id,
        state="ignored",
        completed_at_unix_nano=32_000_000_000,
        lease_token=reclaimed.lease_token or 0,
    )
    replay = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_key,
        body_sha256="a" * 64,
        event_type="finalized",
        now_unix_nano=33_000_000_000,
    )
    assert replay.disposition == "replayed"
    assert replay.lease_token is None


def test_failed_receipt_can_retry_with_a_new_lease_token(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    connector = _connector(store)
    delivery_key = store.fingerprint("delivery:test", "delivery-2")
    first = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_key,
        body_sha256="b" * 64,
        event_type="finalized",
        now_unix_nano=1_000_000_000,
    )
    store.fail_delivery(
        first.receipt_id,
        lease_token=first.lease_token or 0,
        failure_code="INTERRUPTED",
    )

    retry = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_key,
        body_sha256="b" * 64,
        event_type="finalized",
        now_unix_nano=2_000_000_000,
    )

    assert retry.receipt_id == first.receipt_id
    assert retry.lease_token == 2


def test_receipt_and_external_identity_cannot_bind_another_projects_incident(
    tmp_path,
) -> None:
    store = IncidentStore(tmp_path)
    connector = _connector(store)
    store.create_project("other", display_name="Other")
    bundle = make_valid_bundle(bundle_id="other-project-incident")
    result = store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id="other",
    )
    claim = store.claim_delivery(
        connector,
        delivery_key_hmac=store.fingerprint("delivery:test", "cross-project"),
        body_sha256="c" * 64,
        event_type="finalized",
        now_unix_nano=1_000_000_000,
    )

    with pytest.raises(StorageError):
        store.complete_delivery(
            claim.receipt_id,
            state="applied",
            completed_at_unix_nano=2_000_000_000,
            lease_token=claim.lease_token or 0,
            bundle_id=result.record.bundle_id,
            canonical_sha256=result.record.digest,
        )
    with pytest.raises(StorageError):
        store.record_external_identity(
            connector,
            key_kind="call_id",
            value_hmac=store.fingerprint("identity:test", "cross-project"),
            bundle_id=result.record.bundle_id,
        )


def test_claim_delivery_rejects_a_forged_connector_project(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    connector = _connector(store)
    store.create_project("other", display_name="Other")

    with pytest.raises(StorageError):
        store.claim_delivery(
            replace(connector, project_id="other"),
            delivery_key_hmac=store.fingerprint("delivery:test", "forged-project"),
            body_sha256="d" * 64,
            event_type="finalized",
            now_unix_nano=1_000_000_000,
        )
