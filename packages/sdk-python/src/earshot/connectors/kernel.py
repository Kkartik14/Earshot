"""One connector delivery, from its endpoint to its call: the webhook door of ingestion.

Every hook path answers in this order (each step's refusal is final):

1. **Connector** (``endpoint``): an unknown or disabled endpoint is 404, before any
   body byte is read; a connector whose secret is not configured is 503.
2. **Header credential** (``open_body``): a provider that authenticates by a header
   secret (Vapi, Ringg) is checked here, still before the body: 401.
3. **Body** (``DeliveryBody.feed``): kept whole up to the adapter's cap. Past it, a
   provider with events that may be larger (ElevenLabs ``post_call_audio``) streams
   the rest into its verifier, never into memory, up to its streamed limit; any
   other body past the cap, or any past the streamed limit, is 413.
4. **Signature** (``deliver``): the body's signature and timestamp window, 401.
5. **Oversized body**: an authenticated streamed event (its type read from the
   first 4 KiB, a top-level ``type``) is answered 200 ``ignored``; any other is 413.
6. **Strict JSON**, then the event type: 400 if either is malformed.
7. **Not consumed**: 200 ``ignored``, with nothing written and no rate debit.
8. **Rate** per endpoint: 429.
9. **Translate** (pure): 400 for a malformed payload; 422
   ``EARSHOT_CONNECTOR_ID_INVALID`` for a call ID that is not a session key.
10. **Claim** the receipt (``storage_deliveries``): ``replayed``, 409 or 503.
11. **Apply**: spans, recordings, fetches and the receipt in one mutation; 200
    ``{receipt_id, disposition, call_id}``.

**Session keys.** A webhook connector keys its evidence ``"<provider>:<call id>"``,
so it never writes into a call a host keyed by the bare provider ID, unless the
connector sets ``merge_session_keys``. **Recordings** are fetched by a job, never in
the request: from the provider's API by call ID when the connector has an
``api_key_ref`` and the provider serves recordings that way, else from the payload
URL. Under ``metadata_only`` none is held.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..call_capture import valid_session_key
from ..storage import DATABASE_ERRORS, StorageError
from ..storage_deliveries import (
    DeliveryDigests,
    DeliveryInProgressError,
    DeliveryReceiptConflictError,
    DeliveryStore,
    ProviderRecording,
)
from .config import ConnectorConfig, connector_config
from .elevenlabs import ElevenLabsConnectorAdapter
from .evidence import Evidence, otlp_spans
from .retell import RetellConnectorAdapter
from .ringg import RinggConnectorAdapter
from .types import (
    DEFAULT_MAX_BODY_BYTES,
    BodyVerifier,
    ConnectorAdapter,
    ConnectorConfigurationError,
    ConnectorNotFoundError,
    DeliveryBusyError,
    DeliveryConflictError,
    DeliveryError,
    DeliveryIdentifierError,
    DeliveryMappingError,
    DeliveryOutcome,
    DeliveryPayloadError,
    DeliveryPublicationError,
    DeliveryRateLimitedError,
    DeliveryTooLargeError,
    RawProviderDelivery,
    SecretResolver,
)
from .vapi import VapiConnectorAdapter

if TYPE_CHECKING:
    from ..storage import ConnectorRecord, IncidentStore
    from ..storage_calls import CallStore

# How much of an oversized body is kept to read its event type from.
SNIFF_PREFIX_BYTES = 4096
_STORE_FAILURES = (StorageError, *DATABASE_ERRORS, OSError)


class EnvironmentSecretResolver:
    def resolve(self, reference: str) -> tuple[str, ...]:
        if not reference.startswith("env:"):
            return ()
        name = reference.removeprefix("env:")
        values = (os.environ.get(name), os.environ.get(f"{name}_PREVIOUS"))
        return tuple(dict.fromkeys(value for value in values if value))


def strict_json(payload: bytes, *, maximum_depth: int) -> dict[str, Any]:
    """A JSON object with no duplicate keys, no non-finite constants and at most
    ``maximum_depth`` levels; ``DeliveryPayloadError`` otherwise."""

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


def top_level_string(prefix: bytes, key: str) -> str | None:
    """The string value of ``key`` among the top-level members a JSON object's first
    bytes (``prefix``) show in full; ``None`` when they do not show it."""

    text = prefix.decode("utf-8", errors="ignore")
    decoder = json.JSONDecoder()

    def skip(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    try:
        index = skip(0)
        if text[index] != "{":
            return None
        index += 1
        while True:
            index = skip(index)
            member, index = decoder.raw_decode(text, index)
            index = skip(index)
            if not isinstance(member, str) or text[index] != ":":
                return None
            value, index = decoder.raw_decode(text, skip(index + 1))
            if member == key:
                return value if isinstance(value, str) else None
            index = skip(index)
            if text[index] != ",":
                return None
            index += 1
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True, slots=True)
class ConnectorEndpoint:
    """A hook's connector, resolved before its body is read."""

    connector: ConnectorRecord
    adapter: ConnectorAdapter
    config: ConnectorConfig
    secrets: tuple[str, ...] = field(repr=False)
    max_body_bytes: int
    max_streamed_bytes: int


class DeliveryBody:
    """A delivery's body as it streams in: kept whole up to the endpoint's cap, and
    past it only authenticated (and its first 4 KiB kept), up to the streamed limit."""

    def __init__(self, endpoint: ConnectorEndpoint, verifier: BodyVerifier) -> None:
        self.endpoint = endpoint
        self.verifier = verifier
        self.size = 0
        self._kept = bytearray()
        self._prefix = b""
        self.oversized = False

    def declare_length(self, length: int) -> None:
        """Refuse a declared ``Content-Length`` no body of this endpoint may have."""

        if length > self.endpoint.max_streamed_bytes:
            raise DeliveryTooLargeError

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.endpoint.max_streamed_bytes:
            raise DeliveryTooLargeError
        self.verifier.update(chunk)
        if self.oversized:
            return
        self._kept.extend(chunk)
        if len(self._kept) > self.endpoint.max_body_bytes:
            self.oversized = True
            self._prefix = bytes(self._kept[:SNIFF_PREFIX_BYTES])
            self._kept = bytearray()

    @property
    def data(self) -> bytes:
        return bytes(self._kept)

    @property
    def prefix(self) -> bytes:
        return self._prefix if self.oversized else bytes(self._kept[:SNIFF_PREFIX_BYTES])


@dataclass(frozen=True, slots=True)
class _Translated:
    event_type: str
    evidence: Evidence
    session_key: str
    recordings: tuple[ProviderRecording, ...]


class HostedProviderIngestion:
    """Authenticate, translate, govern, deduplicate and apply one provider delivery."""

    def __init__(
        self,
        store: IncidentStore,
        *,
        calls: CallStore | None = None,
        secrets: SecretResolver | None = None,
        now_unix_seconds: Any = None,
        now_monotonic: Any = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_json_depth: int = 64,
        max_deliveries_per_minute: int = 120,
    ) -> None:
        from ..storage_calls import CallStore

        if max_deliveries_per_minute < 1:
            raise ValueError("max_deliveries_per_minute must be positive")
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self._store = store
        self._deliveries = DeliveryStore(store, calls=calls or CallStore(store))
        self._secrets = secrets or EnvironmentSecretResolver()
        self._now_seconds = now_unix_seconds or (lambda: store._clock.unix_nano() // 1_000_000_000)
        self._now_monotonic = now_monotonic or time.monotonic
        self._max_body_bytes = max_body_bytes
        self._max_json_depth = max_json_depth
        self._max_deliveries_per_minute = max_deliveries_per_minute
        self._rate_lock = threading.Lock()
        self._delivery_times: dict[str, deque[float]] = {}
        self._adapters: dict[str, ConnectorAdapter] = {
            adapter.provider: adapter
            for adapter in (
                ElevenLabsConnectorAdapter(),
                RetellConnectorAdapter(),
                RinggConnectorAdapter(),
                VapiConnectorAdapter(),
            )
        }

    @property
    def deliveries(self) -> DeliveryStore:
        return self._deliveries

    # --- the three steps a hook route takes ----------------------------------------------

    def endpoint(self, endpoint_id: str) -> ConnectorEndpoint:
        connector = self._store.get_connector(endpoint_id)
        if connector is None or not connector.enabled:
            raise ConnectorNotFoundError
        adapter = self._adapters.get(connector.provider)
        if adapter is None:
            raise ConnectorNotFoundError
        try:
            config = connector_config(connector.config_json)
        except ValueError as error:
            raise ConnectorConfigurationError from error
        secrets = self._secrets.resolve(connector.secret_ref)
        if not secrets:
            raise ConnectorConfigurationError
        cap = min(adapter.max_body_bytes, self._max_body_bytes)
        return ConnectorEndpoint(
            connector=connector,
            adapter=adapter,
            config=config,
            secrets=secrets,
            max_body_bytes=cap,
            # Past the cap only an adapter with streamed events reads on.
            max_streamed_bytes=(
                max(adapter.max_streamed_bytes, cap) if adapter.streamed_events else cap
            ),
        )

    def open_body(
        self, endpoint: ConnectorEndpoint, headers: tuple[tuple[bytes, bytes], ...]
    ) -> DeliveryBody:
        verifier = endpoint.adapter.verifier(
            headers, endpoint.secrets, now=int(self._now_seconds())
        )
        return DeliveryBody(endpoint, verifier)

    def deliver(self, endpoint: ConnectorEndpoint, body: DeliveryBody) -> DeliveryOutcome:
        connector, adapter = endpoint.connector, endpoint.adapter
        body.verifier.verify()
        if body.oversized:
            if top_level_string(body.prefix, "type") in adapter.streamed_events:
                return self._ignored(connector)
            raise DeliveryTooLargeError
        if body.size == 0:
            raise DeliveryPayloadError
        raw = body.data
        payload = strict_json(raw, maximum_depth=self._max_json_depth)
        event_type = adapter.event_type(payload)
        if event_type not in adapter.consumed_events:
            return self._ignored(connector)
        self._check_rate(connector.endpoint_id)
        translated = self._translate(endpoint, event_type, payload)
        return self._apply(
            endpoint,
            translated,
            DeliveryDigests(
                evidence=translated.evidence.digest(), body=hashlib.sha256(raw).hexdigest()
            ),
        )

    def receive(self, delivery: RawProviderDelivery) -> DeliveryOutcome:
        """All three steps for a body already in memory (what a route streams)."""

        endpoint = self.endpoint(delivery.endpoint_id)
        body = self.open_body(endpoint, delivery.headers)
        body.declare_length(len(delivery.body))
        body.feed(delivery.body)
        return self.deliver(endpoint, body)

    # --- helpers -----------------------------------------------------------------------

    @staticmethod
    def _ignored(connector: ConnectorRecord) -> DeliveryOutcome:
        return DeliveryOutcome(None, "ignored", connector.project_id, None)

    def _translate(
        self, endpoint: ConnectorEndpoint, event_type: str, payload: Mapping[str, Any]
    ) -> _Translated:
        adapter = endpoint.adapter
        try:
            evidence = adapter.translate(event_type, payload)
        except DeliveryError:
            raise
        except (ValueError, OverflowError) as error:
            # A value out of the range evidence allows (a time past 2^63, say): the
            # payload is malformed, and a retry of it would be too.
            raise DeliveryPayloadError from error
        except Exception as error:
            raise DeliveryMappingError from error
        call_id = evidence.provider_call_id
        session_key = (
            call_id if endpoint.config.merge_session_keys else f"{adapter.provider}:{call_id}"
        )
        if valid_session_key(session_key) != session_key:
            raise DeliveryIdentifierError
        recordings = []
        for source in evidence.recording_sources:
            if (
                source.provider_api
                and adapter.serves_recordings_by_id
                and (endpoint.config.api_key_ref is not None)
            ):
                kind, url, expires = "provider_api", None, None
            elif source.url is not None:
                kind, url, expires = "payload_url", source.url, source.url_expires_unix_nano
            else:
                continue
            recordings.append(
                ProviderRecording(
                    provider_call_id=call_id,
                    track=source.track,
                    speaker=source.speaker,
                    content_type=source.content_type,
                    started_at_unix_nano=source.started_at_unix_nano,
                    source_kind=kind,
                    url=url,
                    url_expires_unix_nano=expires,
                )
            )
        return _Translated(event_type, evidence, session_key, tuple(recordings))

    def _apply(
        self, endpoint: ConnectorEndpoint, translated: _Translated, digests: DeliveryDigests
    ) -> DeliveryOutcome:
        connector, adapter = endpoint.connector, endpoint.adapter
        try:
            spans = otlp_spans(
                translated.evidence,
                session_key=translated.session_key,
                translator_version=adapter.translator_version,
            )
        except Exception as error:
            raise DeliveryMappingError from error
        delivery_key = adapter.delivery_key(
            translated.event_type, translated.evidence.provider_call_id
        )
        try:
            claim = self._deliveries.claim(
                connector,
                delivery_key_hmac=self._store.fingerprint(
                    f"delivery:{connector.endpoint_id}", delivery_key
                ),
                digests=digests,
                event_type=translated.event_type,
                now_unix_nano=self._store._clock.unix_nano(),
            )
        except DeliveryReceiptConflictError as error:
            raise DeliveryConflictError from error
        except DeliveryInProgressError as error:
            raise DeliveryBusyError(retry_after_seconds=error.retry_after_seconds) from error
        if claim.disposition == "replayed":
            try:
                self._deliveries.refresh_fetch_urls(connector, translated.recordings)
            except _STORE_FAILURES as error:
                raise DeliveryPublicationError from error
            return DeliveryOutcome(
                claim.receipt_id, "replayed", connector.project_id, claim.call_id
            )
        assert claim.lease_token is not None
        try:
            applied = self._deliveries.apply(
                connector,
                claim.receipt_id,
                lease_token=claim.lease_token,
                session_key=translated.session_key,
                spans=spans,
                recordings=translated.recordings,
            )
        except _STORE_FAILURES as error:
            self._fail_claim(claim.receipt_id, claim.lease_token, "PUBLICATION_FAILED")
            raise DeliveryPublicationError from error
        except Exception as error:
            self._fail_claim(claim.receipt_id, claim.lease_token, "APPLY_FAILED")
            raise DeliveryMappingError from error
        return DeliveryOutcome(
            claim.receipt_id,
            "purged" if applied.purged else "applied",
            connector.project_id,
            applied.call_id,
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

    def _fail_claim(self, receipt_id: str, lease_token: int, failure_code: str) -> None:
        try:
            self._deliveries.fail(receipt_id, lease_token=lease_token, failure_code=failure_code)
        except _STORE_FAILURES:
            # Keep the original error. A lost lease must never let a stale worker
            # overwrite the current owner's receipt.
            return
