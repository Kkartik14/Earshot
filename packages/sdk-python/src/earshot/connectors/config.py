"""A connector's configuration: what ``connectors.config_json`` may hold.

- ``merge_session_keys`` (bool, default false for webhook connectors): a webhook
  connector's evidence is keyed ``"<provider>:<call id>"``, so it never lands in a
  call a host's own telemetry keyed by the bare provider ID. Set it to key by the
  bare ID instead, and merge the provider's evidence into that call (a host that
  stamps ``earshot.session.id = <provider call id>`` on its spans).
- ``api_key_ref`` (``env:NAME``): the provider API key the recording fetch uses
  where the provider's own API serves a recording by call ID. A reference, never a
  value: it is read from the server's environment and never echoed back.

Unknown keys are refused, so a misspelt setting never silently does nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MERGE_SESSION_KEYS = "merge_session_keys"
API_KEY_REF = "api_key_ref"
_KNOWN = frozenset({MERGE_SESSION_KEYS, API_KEY_REF})
_ENV_REFERENCE = re.compile(r"env:[A-Z][A-Z0-9_]{0,127}")
_MAX_CONFIG_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    merge_session_keys: bool = False
    api_key_ref: str | None = None


def validated_config(values: Mapping[str, Any]) -> ConnectorConfig:
    """The configuration ``values`` describe; ``ValueError`` names what is wrong."""

    unknown = sorted(set(values) - _KNOWN)
    if unknown:
        raise ValueError(f"unknown connector setting(s): {', '.join(unknown)}")
    merge = values.get(MERGE_SESSION_KEYS, False)
    if not isinstance(merge, bool):
        raise ValueError(f"{MERGE_SESSION_KEYS} must be true or false")
    api_key_ref = values.get(API_KEY_REF)
    if api_key_ref is not None and (
        not isinstance(api_key_ref, str) or not _ENV_REFERENCE.fullmatch(api_key_ref)
    ):
        raise ValueError(f"{API_KEY_REF} must name an environment variable (env:NAME)")
    return ConnectorConfig(merge_session_keys=merge, api_key_ref=api_key_ref)


def config_json(values: Mapping[str, Any]) -> str:
    """``values``, validated, as the compact JSON ``connectors.config_json`` stores."""

    validated_config(values)
    encoded = json.dumps(dict(values), separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise ValueError("connector configuration is too large")
    return encoded


def connector_config(stored: str | None) -> ConnectorConfig:
    """A stored ``config_json`` as configuration (``None``: the defaults)."""

    if stored is None:
        return ConnectorConfig()
    values = json.loads(stored)
    if not isinstance(values, dict):
        raise ValueError("connector configuration must be a JSON object")
    return validated_config(values)
