"""Provider-neutral interface types for authenticated hosted deliveries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from earshot.contract import IncidentBundle


@dataclass(frozen=True, slots=True)
class RawProviderDelivery:
    endpoint_id: str
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    receipt_id: str
    disposition: Literal["applied", "replayed", "ignored"]
    project_id: str
    bundle_id: str | None
    canonical_sha256: str | None


@dataclass(frozen=True, slots=True)
class ExternalIdentityInput:
    key_kind: str
    value: str = field(repr=False)
    sensitivity: str = "identity"


@dataclass(frozen=True, slots=True)
class NormalizedProviderDelivery:
    delivery_key: str = field(repr=False)
    event_type: str
    disposition: Literal["incident", "ignore"]
    external_identities: tuple[ExternalIdentityInput, ...]
    bundle: IncidentBundle | None


@dataclass(frozen=True, slots=True)
class NormalizationContext:
    fingerprint: Callable[[str, str], str]


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
    public_message = "connector could not normalize the authenticated payload"


class DeliveryPublicationError(DeliveryError):
    code = "EARSHOT_CONNECTOR_PUBLICATION_FAILED"
    http_status = 503
    retryable = True
    public_message = "connector delivery could not be durably published"
