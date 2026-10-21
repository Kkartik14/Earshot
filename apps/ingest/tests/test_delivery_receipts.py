"""Delivery receipts: which retry is the same delivery, and who may finish it.

A receipt is the provider-facing record of one delivery key. These tests pin the
claim rules on a real catalog of the dialect under test:

- the same digest is a replay; a changed digest on an applied receipt is a
  conflict (409), but on a failed receipt, or on a processing receipt whose lease
  expired (its worker crashed), the retry takes the receipt over, digest included,
  so the next retry of the same content is a replay;
- a receipt 0.3.x wrote holds the sha256 of the raw body (``body_v1``), and a retry
  of the same bytes after the upgrade is still a replay;
- only the current lease holder applies or fails a delivery, and applying writes the
  evidence and the receipt together or not at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans
from earshot.storage import StorageError
from earshot.storage_calls import CallStore
from earshot.storage_deliveries import (
    DeliveryDigests,
    DeliveryInProgressError,
    DeliveryLeaseLostError,
    DeliveryReceiptConflictError,
    DeliveryStore,
)
from store_support import catalog, catalog_rows, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
LEASE = 30 * SECOND
FIRST = DeliveryDigests(evidence="1" * 64, body="a" * 64)
SECOND_CONTENT = DeliveryDigests(evidence="2" * 64, body="b" * 64)


@pytest.fixture
def world(tmp_path):
    store = make_store(tmp_path)
    store.create_project("receipts", display_name="Receipts")
    connector = store.create_connector(
        "receipts",
        provider="vapi",
        secret_ref="env:VAPI_SECRET",
        endpoint_id="receipt_connector_0001",
    )
    deliveries = DeliveryStore(store, calls=CallStore(store))
    return store, connector, deliveries


def _key(store, name: str = "delivery-1") -> str:
    return store.fingerprint("delivery:test", name)


def _claim(deliveries, connector, key, digests=FIRST, *, now=START):
    return deliveries.claim(
        connector,
        delivery_key_hmac=key,
        digests=digests,
        event_type="end-of-call-report",
        now_unix_nano=now,
        lease_nano=LEASE,
    )


def _spans(session_key: str = "vapi:call-1"):
    evidence = Evidence(
        provider="vapi",
        provider_call_id=session_key.removeprefix("vapi:"),
        spans=(EvidenceSpan(CALL, 0, START, START + 10 * SECOND),),
    )
    return otlp_spans(evidence, session_key=session_key, translator_version="test")


def _apply(deliveries, connector, claim, session_key: str = "vapi:call-1"):
    return deliveries.apply(
        connector,
        claim.receipt_id,
        lease_token=claim.lease_token,
        session_key=session_key,
        spans=_spans(session_key),
        recordings=(),
    )


def _receipt(tmp_path):
    return catalog_rows(
        tmp_path,
        "SELECT state, body_sha256, digest_kind, attempt_count, call_id FROM delivery_receipts",
    )


def test_an_expired_lease_is_taken_over_and_only_the_new_holder_finishes(world, tmp_path) -> None:
    store, connector, deliveries = world
    key = _key(store)
    first = _claim(deliveries, connector, key)
    assert (first.disposition, first.lease_token) == ("claimed", 1)

    with pytest.raises(DeliveryInProgressError) as active:
        _claim(deliveries, connector, key, now=START + SECOND)
    assert active.value.retry_after_seconds == 29

    taken = _claim(deliveries, connector, key, now=START + LEASE + SECOND)
    assert (taken.receipt_id, taken.lease_token) == (first.receipt_id, 2)

    with pytest.raises(DeliveryLeaseLostError):
        _apply(deliveries, connector, first)
    with pytest.raises(DeliveryLeaseLostError):
        deliveries.fail(first.receipt_id, lease_token=first.lease_token, failure_code="STALE")
    # The stale holder wrote nothing.
    assert catalog_rows(tmp_path, "SELECT count(*) FROM spans") == [(0,)]

    applied = _apply(deliveries, connector, taken)
    replay = _claim(deliveries, connector, key, now=START + LEASE + 2 * SECOND)

    assert applied.call_id is not None
    assert (replay.disposition, replay.call_id, replay.lease_token) == (
        "replayed",
        applied.call_id,
        None,
    )
    assert _receipt(tmp_path) == [("applied", "1" * 64, "evidence_v1", 2, applied.call_id)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM spans") == [(2,)]


def test_a_changed_digest_takes_over_a_failed_receipt_and_its_next_retry_replays(
    world, tmp_path
) -> None:
    store, connector, deliveries = world
    key = _key(store)
    first = _claim(deliveries, connector, key)
    deliveries.fail(first.receipt_id, lease_token=first.lease_token, failure_code="PUBLICATION")

    retry = _claim(deliveries, connector, key, SECOND_CONTENT, now=START + SECOND)
    assert (retry.receipt_id, retry.disposition, retry.lease_token) == (
        first.receipt_id,
        "claimed",
        2,
    )
    assert _receipt(tmp_path) == [("processing", "2" * 64, "evidence_v1", 2, None)]
    _apply(deliveries, connector, retry)

    again = _claim(deliveries, connector, key, SECOND_CONTENT, now=START + 2 * SECOND)
    assert again.disposition == "replayed"


def test_a_crashed_workers_receipt_is_taken_over_with_the_new_digest(world, tmp_path) -> None:
    store, connector, deliveries = world
    key = _key(store)
    _claim(deliveries, connector, key)  # its worker dies: the receipt stays processing

    with pytest.raises(DeliveryInProgressError):
        _claim(deliveries, connector, key, SECOND_CONTENT, now=START + SECOND)
    retry = _claim(deliveries, connector, key, SECOND_CONTENT, now=START + LEASE + SECOND)

    assert (retry.disposition, retry.lease_token) == ("claimed", 2)
    assert _receipt(tmp_path) == [("processing", "2" * 64, "evidence_v1", 2, None)]


def test_a_changed_digest_on_an_applied_receipt_is_a_conflict(world) -> None:
    store, connector, deliveries = world
    key = _key(store)
    _apply(deliveries, connector, _claim(deliveries, connector, key))

    with pytest.raises(DeliveryReceiptConflictError):
        _claim(deliveries, connector, key, SECOND_CONTENT, now=START + SECOND)


def test_a_receipt_from_0_3_is_matched_by_its_raw_body_digest(world, tmp_path) -> None:
    store, connector, deliveries = world
    body = b'{"message":{"type":"end-of-call-report"}}'
    legacy = DeliveryDigests(evidence="3" * 64, body=hashlib.sha256(body).hexdigest())
    with catalog(tmp_path) as connection:
        for receipt_id, name, state in (
            ("receipt_applied", "legacy-applied", "applied"),
            ("receipt_failed", "legacy-failed", "failed"),
        ):
            connection.execute(
                "INSERT INTO delivery_receipts(receipt_id, project_id, endpoint_id, "
                "delivery_key_hmac, body_sha256, event_type, state, "
                "first_received_at_unix_nano, attempt_count, digest_kind) "
                "VALUES (?, 'receipts', ?, ?, ?, 'end-of-call-report', ?, 1, 1, 'body_v1')",
                (receipt_id, connector.endpoint_id, _key(store, name), legacy.body, state),
            )

    replay = _claim(deliveries, connector, _key(store, "legacy-applied"), legacy)
    with pytest.raises(DeliveryReceiptConflictError):
        _claim(
            deliveries,
            connector,
            _key(store, "legacy-applied"),
            replace(legacy, body="c" * 64),
        )
    retried = _claim(deliveries, connector, _key(store, "legacy-failed"), legacy)

    assert (replay.receipt_id, replay.disposition, replay.call_id) == (
        "receipt_applied",
        "replayed",
        None,
    )
    assert (retried.receipt_id, retried.disposition) == ("receipt_failed", "claimed")
    assert catalog_rows(
        tmp_path,
        "SELECT receipt_id, body_sha256, digest_kind FROM delivery_receipts ORDER BY receipt_id",
    ) == [
        ("receipt_applied", legacy.body, "body_v1"),
        ("receipt_failed", "3" * 64, "evidence_v1"),
    ]


def test_a_claim_refuses_a_connector_that_differs_from_the_stored_one(world) -> None:
    store, connector, deliveries = world
    store.create_project("other", display_name="Other")

    with pytest.raises(StorageError):
        _claim(deliveries, replace(connector, project_id="other"), _key(store, "forged"))
