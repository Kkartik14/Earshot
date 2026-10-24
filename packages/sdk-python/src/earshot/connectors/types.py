"""Provider-neutral types for authenticated hosted deliveries, and their errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .evidence import Evidence

# The buffered body every adapter accepts unless it states its own cap.
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RawProviderDelivery:
    endpoint_id: str
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What a hook answers: ``{receipt_id, disposition, call_id}``.

    ``ignored`` deliveries (events no translator consumes) write nothing and have no
    receipt; ``purged`` ones were applied to a call a purge removed (no call).
    """

    receipt_id: str | None
    disposition: Literal["applied", "replayed", "ignored", "purged"]
    project_id: str
    call_id: str | None


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> tuple[str, ...]: ...


class MappingSecretResolver:
    def __init__(self, values: Mapping[str, str | tuple[str, ...]]) -> None:
        self._values = dict(values)

    def resolve(self, reference: str) -> tuple[str, ...]:
        value = self._values.get(reference)
        if value is None:
            return ()
        return (value,) if isinstance(value, str) else value


class BodyVerifier(Protocol):
    """Authenticates a delivery's body as it streams in, without keeping it."""

    def update(self, chunk: bytes) -> None: ...

    def verify(self) -> None:
        """Raise ``DeliveryTrustError`` unless the body read so far is authentic."""


class ConnectorAdapter(Protocol):
    """One provider: how its deliveries authenticate, which it consumes, and how one
    becomes evidence. ``translate`` is pure: no I/O, no clock."""

    provider: str
    translator_version: str
    # The events this adapter turns into evidence; every other authenticated event
    # is answered ``ignored`` and written nowhere.
    consumed_events: frozenset[str]
    # Events allowed past ``max_body_bytes`` (up to ``max_streamed_bytes``): their
    # body is authenticated as it streams and never kept, and they are ignored.
    streamed_events: frozenset[str]
    max_body_bytes: int
    max_streamed_bytes: int
    # Whether the provider's own API serves a recording by call ID (with a key).
    serves_recordings_by_id: bool

    def verifier(
        self, headers: tuple[tuple[bytes, bytes], ...], secrets: tuple[str, ...], *, now: int
    ) -> BodyVerifier: ...

    def event_type(self, payload: Mapping[str, Any]) -> str: ...

    def delivery_key(self, event_type: str, provider_call_id: str) -> str: ...

    def translate(self, event_type: str, payload: Mapping[str, Any]) -> Evidence: ...


class DeliveryError(RuntimeError):
    code = "EARSHOT_CONNECTOR_UNAVAILABLE"
    http_status = 503
    retryable = True
    retry_after_seconds: int | None = None
    public_message = "connector delivery could not be processed"


class ConnectorNotFoundError(DeliveryError):
    code = "EARSHOT_CONNECTOR_NOT_FOUND"
    http_status = 404
    retryable = False
    public_message = "connector endpoint was not found"


class ConnectorConfigurationError(DeliveryError):
    code = "EARSHOT_CONNECTOR_CONFIGURATION_INVALID"
    http_status = 503
    retryable = True
    retry_after_seconds = 30
    public_message = "connector endpoint configuration is incompatible"


class DeliveryTrustError(DeliveryError):
    code = "EARSHOT_CONNECTOR_AUTH_FAILED"
    http_status = 401
    retryable = False
    public_message = "connector signature verification failed"


class DeliveryPayloadError(DeliveryError):
    code = "EARSHOT_CONNECTOR_MALFORMED"
    http_status = 400
    retryable = False
    public_message = "authenticated connector payload is malformed"


class DeliveryIdentifierError(DeliveryError):
    code = "EARSHOT_CONNECTOR_ID_INVALID"
    http_status = 422
    retryable = False
    public_message = "the provider call ID is not a valid session key (1 to 256 UTF-8 bytes)"


class DeliveryTooLargeError(DeliveryError):
    code = "EARSHOT_CONNECTOR_BODY_TOO_LARGE"
    http_status = 413
    retryable = False
    public_message = "connector payload exceeds the configured limit"


class DeliveryRateLimitedError(DeliveryError):
    code = "EARSHOT_CONNECTOR_RATE_LIMITED"
    http_status = 429
    retryable = True
    public_message = "connector delivery rate limit exceeded"

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__(self.public_message)
        self.retry_after_seconds = retry_after_seconds


class DeliveryConflictError(DeliveryError):
    code = "EARSHOT_DELIVERY_CONFLICT"
    http_status = 409
    retryable = False
    public_message = "connector delivery identity conflicts with prior content"


class DeliveryBusyError(DeliveryError):
    code = "EARSHOT_CONNECTOR_UNAVAILABLE"
    http_status = 503
    retryable = True
    public_message = "connector delivery is already processing"

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__(self.public_message)
        self.retry_after_seconds = retry_after_seconds


class DeliveryMappingError(DeliveryError):
    code = "EARSHOT_CONNECTOR_MAPPING_FAILED"
    http_status = 500
    retryable = True
    public_message = "connector could not translate the authenticated payload"


class DeliveryPublicationError(DeliveryError):
    code = "EARSHOT_CONNECTOR_PUBLICATION_FAILED"
    http_status = 503
    retryable = True
    public_message = "connector delivery could not be durably published"
