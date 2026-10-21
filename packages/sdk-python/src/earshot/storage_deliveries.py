"""Connector deliveries in the catalog: receipts, and one delivery's evidence in one mutation.

A ``DeliveryStore`` is the storage half of a connector hook. It runs on an
``IncidentStore``'s database, write locks and clock, and uses the ``CallStore``
for everything a call is (spans, identity, tombstones, the close rules).

**Receipts** (``delivery_receipts``) are the provider-facing record of one delivery
key (``endpoint_id``, ``delivery_key_hmac``). ``claim`` decides what a delivery is:

======================================  ===========================================
stored receipt                          outcome
======================================  ===========================================
none                                    a new receipt, ``processing``, leased
``applied``/``ignored``, same digest    ``replayed``: nothing is written again
``applied``/``ignored``, other digest   ``DeliveryReceiptConflictError`` (409)
``processing``, lease not expired       ``DeliveryInProgressError`` (503)
``failed``, or ``processing`` expired   taken over: leased again under the next
                                        ``attempt_count``, with this delivery's
                                        digest, event type and digest kind
======================================  ===========================================

The digest compared is the one the receipt's ``digest_kind`` names: ``evidence_v1``
(the delivery's canonical evidence and recording locators, what every new receipt
stores) or ``body_v1`` (the sha256 of the raw body, what 0.3.x stored). So a retry
whose payload differs only in re-signed URLs is a replay, and so is a retry, after
an upgrade, of a delivery 0.3.x applied. A takeover rewrites the digest in the same
update, so the next retry of the same content replays. The lease token is the
receipt's ``attempt_count``: only its holder applies or fails it.

**Apply** writes one delivery in one mutation: its spans (``CallStore.record_spans_in``),
its provider recordings and their fetch jobs, and the receipt, ``applied`` and
bound to the call, all under the lease, or none of it. A session key a call purge
tombstoned writes nothing but the receipt (applied, bound to no call: ``purged``).
Under ``metadata_only`` only the spans are written. A provider recording's ID is a
function of the project, provider, call ID and track, and its manifest of the
payload: a recording that exists, in any state, gains no row and no fetch, though a
fetch that has not finished takes the delivery's fresher URL.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .call_capture import CAPTURE_PRESET_FULL
from .call_materializer import MAX_REVISION_RECORDINGS
from .storage import StorageError, _connector_record

if TYPE_CHECKING:
    from .call_capture import OtlpSpan
    from .storage import ConnectorRecord, IncidentStore
    from .storage_calls import CallStore
    from .storage_database import DatabaseConnection

DIGEST_BODY = "body_v1"
DIGEST_EVIDENCE = "evidence_v1"
DEFAULT_LEASE_NANO = 30_000_000_000
_RECORDING_NAMESPACE = "connector-recording-v1"
# Fetch states that still use their URL; the others are terminal.
_LIVE_FETCH_STATES = ("pending", "running")


class DeliveryReceiptConflictError(StorageError):
    """The delivery key was already applied with other content."""


class DeliveryInProgressError(StorageError):
    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class DeliveryLeaseLostError(StorageError):
    """The caller no longer holds the receipt's lease: another attempt took it over."""


@dataclass(frozen=True, slots=True)
class DeliveryDigests:
    """A delivery's digests: of its evidence (stored), and of its raw body (compared
    only against a receipt 0.3.x wrote)."""

    evidence: str
    body: str


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    receipt_id: str
    disposition: Literal["claimed", "replayed"]
    # A replay's call (``None``: a purged call's delivery, or a 0.3.x receipt).
    call_id: str | None
    # The claim's lease token; ``None`` for a replay.
    lease_token: int | None


@dataclass(frozen=True, slots=True)
class ProviderRecording:
    """A provider recording a delivery holds for its call, and where its fetch reads.

    ``source_kind`` is ``payload_url`` (``url`` is the payload's, a bearer
    capability) or ``provider_api`` (the fetch builds the URL from the call ID on
    the provider's API host; ``url`` is ``None``).
    """

    provider_call_id: str
    track: str
    speaker: str
    content_type: str
    started_at_unix_nano: int
    source_kind: str
    url: str | None = None
    url_expires_unix_nano: int | None = None


@dataclass(frozen=True, slots=True)
class AppliedDelivery:
    # The call the evidence went to; ``None`` when its key's call was purged.
    call_id: str | None
    recordings_created: tuple[str, ...] = ()

    @property
    def purged(self) -> bool:
        return self.call_id is None


class DeliveryStore:
    """Receipt and delivery storage on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore, *, calls: CallStore) -> None:
        self.store = store
        self.calls = calls

    def recording_id(
        self, project_id: str, provider: str, provider_call_id: str, track: str
    ) -> str:
        """A provider recording's ID: ``conn_`` and 40 hex digits of an HMAC of
        (project, provider, call ID, track), so every delivery names the same one."""

        value = f"{project_id}\x00{provider}\x00{provider_call_id}\x00{track}"
        return "conn_" + self.store.fingerprint(_RECORDING_NAMESPACE, value)[:40]

    # --- receipts ------------------------------------------------------------------------

    def claim(
        self,
        connector: ConnectorRecord,
        *,
        delivery_key_hmac: str,
        digests: DeliveryDigests,
        event_type: str,
        now_unix_nano: int,
        lease_nano: int = DEFAULT_LEASE_NANO,
    ) -> DeliveryClaim:
        """What this delivery is (the table in the module docstring); a claim leases
        its receipt until ``now + lease_nano``."""

        with self.store._mutation("claim_delivery"), self.store._connect() as connection:
            connection.begin_write()
            self._require_connector(connection, connector)
            row = connection.execute(
                "SELECT receipt_id, state, body_sha256, digest_kind, attempt_count, "
                "lease_until_unix_nano, call_id FROM delivery_receipts "
                "WHERE endpoint_id = ? AND delivery_key_hmac = ?",
                (connector.endpoint_id, delivery_key_hmac),
            ).fetchone()
            if row is None:
                receipt_id = f"receipt_{secrets.token_urlsafe(18)}"
                connection.execute(
                    "INSERT INTO delivery_receipts(receipt_id, project_id, endpoint_id, "
                    "delivery_key_hmac, body_sha256, digest_kind, event_type, state, "
                    "first_received_at_unix_nano, lease_until_unix_nano, attempt_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?, 1)",
                    (
                        receipt_id,
                        connector.project_id,
                        connector.endpoint_id,
                        delivery_key_hmac,
                        digests.evidence,
                        DIGEST_EVIDENCE,
                        event_type,
                        now_unix_nano,
                        now_unix_nano + lease_nano,
                    ),
                )
                connection.commit()
                return DeliveryClaim(receipt_id, "claimed", None, 1)
            stored = digests.body if row["digest_kind"] == DIGEST_BODY else digests.evidence
            if row["state"] in {"applied", "ignored"}:
                if row["body_sha256"] != stored:
                    raise DeliveryReceiptConflictError("delivery key was reused with new content")
                return DeliveryClaim(row["receipt_id"], "replayed", row["call_id"], None)
            lease_until = row["lease_until_unix_nano"]
            if row["state"] == "processing" and (lease_until or 0) > now_unix_nano:
                raise DeliveryInProgressError(
                    "delivery is already being processed",
                    retry_after_seconds=max(
                        1, (lease_until - now_unix_nano + 999_999_999) // 1_000_000_000
                    ),
                )
            token = int(row["attempt_count"]) + 1
            taken = connection.execute(
                "UPDATE delivery_receipts SET state = 'processing', lease_until_unix_nano = ?, "
                "attempt_count = ?, failure_code = NULL, body_sha256 = ?, digest_kind = ?, "
                "event_type = ? WHERE receipt_id = ? AND attempt_count = ?",
                (
                    now_unix_nano + lease_nano,
                    token,
                    digests.evidence,
                    DIGEST_EVIDENCE,
                    event_type,
                    row["receipt_id"],
                    token - 1,
                ),
            ).rowcount
            if taken != 1:  # pragma: no cover - the write lock serializes claims
                raise StorageError("delivery receipt changed under the store write lock")
            connection.commit()
            return DeliveryClaim(row["receipt_id"], "claimed", None, token)

    def fail(self, receipt_id: str, *, lease_token: int, failure_code: str) -> None:
        """Mark a claimed delivery failed (its retry takes the receipt over)."""

        with self.store._mutation("fail_delivery"), self.store._connect() as connection:
            connection.begin_write()
            self._require_receipt_project(connection, receipt_id)
            failed = connection.execute(
                "UPDATE delivery_receipts SET state = 'failed', lease_until_unix_nano = NULL, "
                "failure_code = ? WHERE receipt_id = ? AND state = 'processing' "
                "AND attempt_count = ?",
                (failure_code, receipt_id, lease_token),
            ).rowcount
            if failed != 1:
                raise DeliveryLeaseLostError("delivery receipt lease is no longer owned")
            connection.commit()

    # --- one delivery, one mutation ----------------------------------------------------

    def apply(
        self,
        connector: ConnectorRecord,
        receipt_id: str,
        *,
        lease_token: int,
        session_key: str,
        spans: Sequence[OtlpSpan],
        recordings: Sequence[ProviderRecording],
        budget_s: float | None = None,
    ) -> AppliedDelivery:
        """Write a claimed delivery's evidence and finish its receipt, in one mutation."""

        project_id = connector.project_id
        with (
            self.store._mutation("apply_delivery", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            owned = connection.execute(
                "SELECT 1 FROM delivery_receipts WHERE receipt_id = ? AND endpoint_id = ? "
                "AND state = 'processing' AND attempt_count = ?",
                (receipt_id, connector.endpoint_id, lease_token),
            ).fetchone()
            if owned is None:
                raise DeliveryLeaseLostError("delivery receipt lease is no longer owned")
            preset = self.calls._read_preset(connection, project_id)
            now = self.store._clock.unix_nano()
            fingerprint = self.calls.key_fingerprint(project_id, session_key)
            if self.calls._tombstoned(connection, project_id, fingerprint, now):
                self.calls._bump_counters(
                    connection, project_id, purged_call_evidence_dropped=len(spans)
                )
                self._finish(connection, receipt_id, lease_token, None, now)
                connection.commit()
                return AppliedDelivery(None)
            self.calls.record_spans_in(connection, project_id, spans, now=now)
            generation = self.calls._generation(connection, project_id, fingerprint)
            call_id = self.calls.call_id(project_id, session_key, generation)
            created: list[str] = []
            if preset == CAPTURE_PRESET_FULL:
                created = self._hold_recordings(connection, connector, call_id, recordings, now)
            self._finish(connection, receipt_id, lease_token, call_id, now)
            connection.commit()
        return AppliedDelivery(call_id, tuple(created))

    def refresh_fetch_urls(
        self,
        connector: ConnectorRecord,
        recordings: Sequence[ProviderRecording],
        *,
        budget_s: float | None = None,
    ) -> int:
        """Give each unfinished payload-URL fetch of ``recordings`` the URL a replayed
        delivery carries (a provider re-signs its URLs on every delivery). Returns how
        many fetches took a new URL."""

        fresh = [recording for recording in recordings if recording.url is not None]
        if not fresh:
            return 0
        with (
            self.store._mutation("refresh_fetch_urls", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            now = self.store._clock.unix_nano()
            refreshed = sum(
                self._refresh_url(connection, connector, recording, now) for recording in fresh
            )
            connection.commit()
        return refreshed

    # --- helpers -----------------------------------------------------------------------

    def _require_connector(self, connection: DatabaseConnection, connector: ConnectorRecord):
        self.store._require_active_project(connection, connector.project_id)
        row = connection.execute(
            "SELECT * FROM connectors WHERE endpoint_id = ?", (connector.endpoint_id,)
        ).fetchone()
        if row is None or _connector_record(row) != connector or not connector.enabled:
            raise StorageError("delivery connector does not match stored configuration")

    def _require_receipt_project(self, connection: DatabaseConnection, receipt_id: str) -> None:
        row = connection.execute(
            "SELECT project_id FROM delivery_receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise DeliveryLeaseLostError("delivery receipt no longer exists")
        self.store._require_active_project(connection, row["project_id"])

    @staticmethod
    def _finish(
        connection: DatabaseConnection,
        receipt_id: str,
        lease_token: int,
        call_id: str | None,
        now: int,
    ) -> None:
        finished = connection.execute(
            "UPDATE delivery_receipts SET state = 'applied', call_id = ?, "
            "completed_at_unix_nano = ?, lease_until_unix_nano = NULL, failure_code = NULL "
            "WHERE receipt_id = ? AND state = 'processing' AND attempt_count = ?",
            (call_id, now, receipt_id, lease_token),
        ).rowcount
        if finished != 1:  # pragma: no cover - checked under the same write lock
            raise DeliveryLeaseLostError("delivery receipt lease is no longer owned")

    def _hold_recordings(
        self,
        connection: DatabaseConnection,
        connector: ConnectorRecord,
        call_id: str,
        recordings: Sequence[ProviderRecording],
        now: int,
    ) -> list[str]:
        project_id = connector.project_id
        created = []
        for recording in recordings:
            recording_id = self.recording_id(
                project_id, connector.provider, recording.provider_call_id, recording.track
            )
            exists = connection.execute(
                "SELECT 1 FROM recordings WHERE project_id = ? AND recording_id = ?",
                (project_id, recording_id),
            ).fetchone()
            if exists is not None:
                self._refresh_url(connection, connector, recording, now)
                continue
            [held] = connection.execute(
                "SELECT count(*) FROM recordings WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ).fetchone()
            if held >= MAX_REVISION_RECORDINGS:
                continue  # a revision holds no more; the provider's copy stays theirs
            connection.execute(
                "INSERT INTO recordings(project_id, recording_id, call_id, speaker, "
                "content_type, label, started_at_unix_nano, state, created_unix_nano) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    project_id,
                    recording_id,
                    call_id,
                    recording.speaker,
                    recording.content_type,
                    f"{connector.provider}:{recording.track}",
                    str(recording.started_at_unix_nano),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO recording_fetches(project_id, recording_id, endpoint_id, "
                "source_kind, provider_call_id, track, url, url_expires_unix_nano, state, "
                "attempts, lease_token, created_unix_nano, updated_unix_nano) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)",
                (
                    project_id,
                    recording_id,
                    connector.endpoint_id,
                    recording.source_kind,
                    recording.provider_call_id,
                    recording.track,
                    recording.url,
                    recording.url_expires_unix_nano,
                    now,
                    now,
                ),
            )
            created.append(recording_id)
        return created

    def _refresh_url(
        self,
        connection: DatabaseConnection,
        connector: ConnectorRecord,
        recording: ProviderRecording,
        now: int,
    ) -> int:
        if recording.url is None:
            return 0
        recording_id = self.recording_id(
            connector.project_id, connector.provider, recording.provider_call_id, recording.track
        )
        marks = ", ".join("?" * len(_LIVE_FETCH_STATES))
        parameters: tuple[Any, ...] = (
            recording.url,
            recording.url_expires_unix_nano,
            now,
            connector.project_id,
            recording_id,
            *_LIVE_FETCH_STATES,
            recording.url,
        )
        return int(
            connection.execute(
                "UPDATE recording_fetches SET url = ?, url_expires_unix_nano = ?, "
                "updated_unix_nano = ? WHERE project_id = ? AND recording_id = ? "
                f"AND source_kind = 'payload_url' AND state IN ({marks}) "
                "AND (url IS NULL OR url <> ?)",
                parameters,
            ).rowcount
        )
