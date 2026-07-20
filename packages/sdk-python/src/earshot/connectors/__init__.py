"""Hosted-provider connector interface."""

from .kernel import EnvironmentSecretResolver, HostedProviderIngestion
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
    DeliveryTrustError,
    MappingSecretResolver,
    RawProviderDelivery,
)

__all__ = [
    "ConnectorConfigurationError",
    "ConnectorNotFoundError",
    "DeliveryBusyError",
    "DeliveryConflictError",
    "DeliveryError",
    "DeliveryMappingError",
    "DeliveryOutcome",
    "DeliveryPayloadError",
    "DeliveryPublicationError",
    "DeliveryRateLimitedError",
    "DeliveryTooLargeError",
    "DeliveryTrustError",
    "EnvironmentSecretResolver",
    "HostedProviderIngestion",
    "MappingSecretResolver",
    "RawProviderDelivery",
]
