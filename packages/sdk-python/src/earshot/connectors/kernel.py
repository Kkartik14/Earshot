"""Deep hosted-provider ingestion module."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Any

from earshot.codec import encode_incident_protobuf
from earshot.storage import (
    DeliveryInProgressError,
    DeliveryReceiptConflictError,
    IncidentStore,
    StorageError,
)

from .elevenlabs import ElevenLabsConnectorAdapter
from .retell import RetellConnectorAdapter
from .types import (
    ConnectorConfigurationError,
    ConnectorNotFoundError,
    DeliveryBusyError,
    DeliveryConflictError,
    DeliveryError,
    DeliveryMappingError,
    DeliveryOutcome,
    DeliveryPayloadError,
    DeliveryPublicationError,
    DeliveryRateLimitedError,
    DeliveryTooLargeError,
    NormalizationContext,
    RawProviderDelivery,
    SecretResolver,
)
from .vapi import VapiConnectorAdapter


class EnvironmentSecretResolver:
    def resolve(self, reference: str) -> tuple[str, ...]:
        if not reference.startswith("env:"):
            return ()
        name = reference.removeprefix("env:")
        values = (os.environ.get(name), os.environ.get(f"{name}_PREVIOUS"))
        return tuple(dict.fromkeys(value for value in values if value))


def _strict_json(payload: bytes, *, maximum_depth: int) -> dict[str, Any]:
    class DuplicateKey(ValueError):
        pass

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in values:
            if key in output:
                raise DuplicateKey
            output[key] = value
        return output

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (DuplicateKey, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DeliveryPayloadError from None
    if not isinstance(value, dict):
        raise DeliveryPayloadError
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum_depth:
            raise DeliveryPayloadError
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return value


class HostedProviderIngestion:
    """Authenticate, normalize, govern, deduplicate, and publish one Provider Delivery."""

    def __init__(
        self,
        store: IncidentStore,
        *,
        secrets: SecretResolver | None = None,
        now_unix_seconds: Any = None,
        now_monotonic: Any = None,
        max_body_bytes: int = 2 * 1024 * 1024,
        max_json_depth: int = 64,
        signature_tolerance_seconds: int = 300,
        max_deliveries_per_minute: int = 120,
    ) -> None:
        if max_deliveries_per_minute < 1:
            raise ValueError("max_deliveries_per_minute must be positive")
        self._store = store
        self._secrets = secrets or EnvironmentSecretResolver()
        self._now_seconds = now_unix_seconds or (lambda: int(time.time()))
        self._now_monotonic = now_monotonic or time.monotonic
        self._max_body_bytes = max_body_bytes
        self._max_json_depth = max_json_depth
        self._signature_tolerance_seconds = signature_tolerance_seconds
        self._max_deliveries_per_minute = max_deliveries_per_minute
        self._rate_lock = threading.Lock()
        self._delivery_times: dict[str, deque[float]] = {}
        self._adapters = {
            "elevenlabs": ElevenLabsConnectorAdapter(),
            "retell": RetellConnectorAdapter(),
            "vapi": VapiConnectorAdapter(),
        }

    def receive(self, delivery: RawProviderDelivery) -> DeliveryOutcome:
        if len(delivery.body) > self._max_body_bytes:
            raise DeliveryTooLargeError
        connector = self._store.get_connector(delivery.endpoint_id)
        if connector is None or not connector.enabled:
            raise ConnectorNotFoundError
        adapter = self._adapters.get(connector.provider)
        if adapter is None:
            raise ConnectorNotFoundError
        if connector.normalizer_version != adapter.normalizer_version:
            raise ConnectorConfigurationError
        secrets = self._secrets.resolve(connector.secret_ref)
        if not secrets:
            raise ConnectorConfigurationError
        adapter.authenticate(
            delivery,
            secrets,
            now_unix_seconds=int(self._now_seconds()),
            tolerance_seconds=self._signature_tolerance_seconds,
        )
        self._check_rate(connector.endpoint_id)
        payload = _strict_json(delivery.body, maximum_depth=self._max_json_depth)

        def connector_fingerprint(namespace: str, value: str) -> str:
            return self._store.fingerprint(
                f"connector:{connector.provider}:{connector.endpoint_id}:{namespace}",
                value,
            )

        try:
            normalized = adapter.normalize(
                payload,
                NormalizationContext(fingerprint=connector_fingerprint),
            )
        except DeliveryError:
            raise
        except Exception as error:
            raise DeliveryMappingError from error
        body_digest = hashlib.sha256(delivery.body).hexdigest()
        delivery_key_hmac = self._store.fingerprint(
            f"delivery:{connector.endpoint_id}", normalized.delivery_key
        )
        now_nano = int(self._now_seconds()) * 1_000_000_000
        try:
            claim = self._store.claim_delivery(
                connector,
                delivery_key_hmac=delivery_key_hmac,
                body_sha256=body_digest,
                event_type=normalized.event_type,
                now_unix_nano=now_nano,
            )
        except DeliveryReceiptConflictError as error:
            raise DeliveryConflictError from error
        except DeliveryInProgressError as error:
            raise DeliveryBusyError(
                retry_after_seconds=error.retry_after_seconds
            ) from error
        if claim.disposition == "replayed":
            return DeliveryOutcome(
                receipt_id=claim.receipt_id,
                disposition="replayed",
                project_id=connector.project_id,
                bundle_id=claim.bundle_id,
                canonical_sha256=claim.canonical_sha256,
            )
        if normalized.disposition == "ignore":
            assert claim.lease_token is not None
            try:
                self._store.complete_delivery(
                    claim.receipt_id,
                    state="ignored",
                    completed_at_unix_nano=now_nano,
                    lease_token=claim.lease_token,
                )
            except (StorageError, sqlite3.Error, OSError) as error:
                raise DeliveryPublicationError from error
            return DeliveryOutcome(
                claim.receipt_id, "ignored", connector.project_id, None, None
            )
        assert claim.lease_token is not None
        if normalized.bundle is None:
            self._fail_claim(
                claim.receipt_id,
                lease_token=claim.lease_token,
                failure_code="NORMALIZER_EMPTY",
            )
            raise DeliveryMappingError
        try:
            canonical = encode_incident_protobuf(normalized.bundle)
        except Exception as error:
            self._fail_claim(
                claim.receipt_id,
                lease_token=claim.lease_token,
                failure_code="NORMALIZATION_FAILED",
            )
            raise DeliveryMappingError from error
        try:
            result = self._store.ingest(
                normalized.bundle,
                canonical,
                project_id=connector.project_id,
            )
            for identity in normalized.external_identities:
                self._store.record_external_identity(
                    connector,
                    key_kind=identity.key_kind,
                    value_hmac=self._store.fingerprint(
                        f"identity:{connector.endpoint_id}:{identity.key_kind}", identity.value
                    ),
                    bundle_id=result.record.bundle_id,
                    sensitivity=identity.sensitivity,
                )
            self._store.complete_delivery(
                claim.receipt_id,
                state="applied",
                completed_at_unix_nano=now_nano,
                lease_token=claim.lease_token,
                bundle_id=result.record.bundle_id,
                canonical_sha256=result.record.digest,
            )
        except (StorageError, sqlite3.Error, OSError) as error:
            self._fail_claim(
                claim.receipt_id,
                lease_token=claim.lease_token,
                failure_code="PUBLICATION_FAILED",
            )
            raise DeliveryPublicationError from error
        except Exception as error:
            self._fail_claim(
                claim.receipt_id,
                lease_token=claim.lease_token,
                failure_code="NORMALIZATION_FAILED",
            )
            raise DeliveryMappingError from error
        return DeliveryOutcome(
            receipt_id=claim.receipt_id,
            disposition="applied",
            project_id=connector.project_id,
            bundle_id=result.record.bundle_id,
            canonical_sha256=result.record.digest,
        )

    def _check_rate(self, endpoint_id: str) -> None:
        now = float(self._now_monotonic())
        cutoff = now - 60.0
        with self._rate_lock:
            times = self._delivery_times.setdefault(endpoint_id, deque())
            while times and times[0] <= cutoff:
                times.popleft()
            if len(times) >= self._max_deliveries_per_minute:
                retry_after = max(1, math.ceil(60.0 - (now - times[0])))
                raise DeliveryRateLimitedError(retry_after_seconds=retry_after)
            times.append(now)

    def _fail_claim(
        self,
        receipt_id: str,
        *,
        lease_token: int,
        failure_code: str,
    ) -> None:
        try:
            self._store.fail_delivery(
                receipt_id,
                lease_token=lease_token,
                failure_code=failure_code,
            )
        except (StorageError, sqlite3.Error, OSError):
            # Preserve the original stable connector error. A lost lease must
            # never let a stale worker overwrite the current owner's Receipt.
            return
