"""Connector tests: a store with a controlled clock, signed deliveries, and catalog reads.

Every delivery is signed with a test secret the way its provider signs (ElevenLabs
and Retell HMACs, Vapi and Ringg header secrets) and goes through the real ingestion
kernel, call store and recordings store on the dialect under test (``--db``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from earshot.blobstore import LocalBlobStore
from earshot.call_capture import stored_span
from earshot.clock import ManualClock
from earshot.connectors import HostedProviderIngestion, MappingSecretResolver, RawProviderDelivery
from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore
from earshot.storage_recordings import RecordingStore
from store_support import catalog_rows, make_store

SECOND = 1_000_000_000
# 2026-10-20T10:00:00Z: the deliveries' call start and the store's clock.
START_SECONDS = 1_792_490_400
START = START_SECONDS * SECOND
ROOT = Path(__file__).resolve().parents[3]
SECRETS = {
    "vapi": "vapi-test-secret",
    "retell": "retell-test-api-key",
    "elevenlabs": "wsec_elevenlabs_test_secret",
    "ringg": "ringg-test-secret",
}
SECRET_REFS = {provider: f"env:{provider.upper()}_TEST_SECRET" for provider in SECRETS}


def body_of(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_headers(provider: str, body: bytes, *, at_seconds: int = START_SECONDS):
    """The headers ``provider`` sends with ``body``, signed with its test secret."""

    secret = SECRETS[provider].encode()
    content_type = (b"content-type", b"application/json")
    if provider == "elevenlabs":
        digest = hmac.new(secret, str(at_seconds).encode() + b"." + body, hashlib.sha256)
        value = f"t={at_seconds},v0={digest.hexdigest()}".encode()
        return (content_type, (b"elevenlabs-signature", value))
    if provider == "retell":
        milliseconds = str(at_seconds * 1_000).encode()
        digest = hmac.new(secret, body + milliseconds, hashlib.sha256)
        return (
            content_type,
            (b"x-retell-signature", b"v=" + milliseconds + b",d=" + digest.hexdigest().encode()),
        )
    return (content_type, (b"authorization", b"Bearer " + secret))


@dataclass
class ConnectorWorld:
    """One project with one connector of a provider, on a store with a manual clock."""

    path: Path
    clock: ManualClock
    store: IncidentStore
    calls: CallStore
    recordings: RecordingStore
    ingestion: HostedProviderIngestion
    project_id: str
    endpoint_id: str
    provider: str

    def deliver(self, payload: Any, *, at_seconds: int | None = None, body: bytes | None = None):
        raw = body_of(payload) if body is None else body
        when = self.clock.unix_nano() // SECOND if at_seconds is None else at_seconds
        return self.ingestion.receive(
            RawProviderDelivery(
                endpoint_id=self.endpoint_id,
                headers=signed_headers(self.provider, raw, at_seconds=when),
                body=raw,
            )
        )

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple]:
        return catalog_rows(self.path, sql, parameters)

    def spans(self) -> list[Any]:
        """Every stored span, read back as an ``OtlpSpan``, in name then span order."""

        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT s.payload, r.payload AS resource FROM spans AS s "
                "JOIN otlp_resources AS r ON r.project_id = s.project_id "
                "AND r.resource_digest = s.resource_digest "
                "WHERE s.project_id = ? ORDER BY s.name, s.span_id",
                (self.project_id,),
            ).fetchall()
        return [stored_span(bytes(row["payload"]), bytes(row["resource"])) for row in rows]

    def span_names(self) -> list[str]:
        return [span.span.name for span in self.spans()]

    def counters(self) -> dict[str, int]:
        return self.calls.otlp_stats(self.project_id)

    def fetches(self) -> list[tuple]:
        return self.rows(
            "SELECT r.speaker, r.content_type, r.state, f.source_kind, f.track, f.url, "
            "f.url_expires_unix_nano, f.state FROM recording_fetches AS f JOIN recordings AS r "
            "ON r.project_id = f.project_id AND r.recording_id = f.recording_id "
            "WHERE f.project_id = ? ORDER BY f.track",
            (self.project_id,),
        )


def connector_world(
    tmp_path: Path,
    provider: str,
    *,
    config: dict[str, Any] | None = None,
    preset: str = "full",
    max_deliveries_per_minute: int = 120,
    max_body_bytes: int = 2 * 1024 * 1024,
) -> ConnectorWorld:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    store.create_project("support", display_name="Support")
    if preset != "full":
        store.set_capture_preset("support", preset)
    connector = store.create_connector(
        "support",
        provider=provider,
        secret_ref=SECRET_REFS[provider],
        endpoint_id=f"{provider}_connector_0001",
        config=config,
    )
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    ingestion = HostedProviderIngestion(
        store,
        calls=calls,
        secrets=MappingSecretResolver({SECRET_REFS[provider]: SECRETS[provider]}),
        now_monotonic=lambda: clock.monotonic_nano() / SECOND,
        max_deliveries_per_minute=max_deliveries_per_minute,
        max_body_bytes=max_body_bytes,
    )
    return ConnectorWorld(
        path=tmp_path,
        clock=clock,
        store=store,
        calls=calls,
        recordings=recordings,
        ingestion=ingestion,
        project_id="support",
        endpoint_id=connector.endpoint_id,
        provider=provider,
    )
