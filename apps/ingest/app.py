"""ASGI entry point for ``uvicorn apps.ingest.app:app``."""

from __future__ import annotations

import os
from pathlib import Path

from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.api import ApiConfig, create_app


def _integer_environment(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value is not None else default


def _float_environment(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value is not None else default


def _set_environment(name: str) -> frozenset[str]:
    value = os.environ.get(name, "")
    return frozenset(item.strip() for item in value.split(",") if item.strip())


HOST = os.environ.get("EARSHOT_HOST", "127.0.0.1")
TOKEN = os.environ.get("EARSHOT_TOKEN")
AUTH_MODE = os.environ.get("EARSHOT_AUTH_MODE", "operator")
JWT_ISSUER = os.environ.get("EARSHOT_JWT_ISSUER")
JWT_AUDIENCE = os.environ.get("EARSHOT_JWT_AUDIENCE")
JWKS_URL = os.environ.get("EARSHOT_JWKS_URL")
JWKS_CA_FILE = os.environ.get("EARSHOT_JWKS_CA_FILE")
PLATFORM_ADAPTER_ENABLED = os.environ.get("EARSHOT_PLATFORM_ADAPTER_ENABLED", "").lower() in {
    "1",
    "true",
    "yes",
}
DATA_DIR = Path(os.environ.get("EARSHOT_DATA_DIR", ".earshot"))
# Opt-in durability for in-flight continuous browser calls. Hosted capture v2
# fails closed unless this names an existing durable directory; operator-mode
# capture may still use in-memory calls when it is unset.
CAPTURE_JOURNAL_DIR = os.environ.get("EARSHOT_CAPTURE_JOURNAL_DIR")
BEHIND_TLS_PROXY = os.environ.get("EARSHOT_BEHIND_TLS_PROXY", "").lower() in {
    "1",
    "true",
    "yes",
}

app = create_app(
    data_dir=DATA_DIR,
    analyzer=analyze_incident,
    capture_journal_dir=CAPTURE_JOURNAL_DIR,
    config=ApiConfig(
        host=HOST,
        token=TOKEN,
        auth_mode=AUTH_MODE,
        jwt_issuer=JWT_ISSUER,
        jwt_audience=JWT_AUDIENCE,
        jwks_url=JWKS_URL,
        jwks_ca_file=JWKS_CA_FILE,
        hosted_runtime_names=_set_environment("EARSHOT_HOSTED_RUNTIME_NAMES"),
        hosted_session_statuses=_set_environment("EARSHOT_HOSTED_SESSION_STATUSES"),
        hosted_event_names=_set_environment("EARSHOT_HOSTED_EVENT_NAMES"),
        max_body_bytes=_integer_environment("EARSHOT_MAX_BODY_BYTES", 16 * 1024 * 1024),
        max_connector_body_bytes=_integer_environment(
            "EARSHOT_MAX_CONNECTOR_BODY_BYTES", 2 * 1024 * 1024
        ),
        max_connector_deliveries_per_minute=_integer_environment(
            "EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE", 120
        ),
        retention_cleanup_interval_seconds=_float_environment(
            "EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS", 5.0
        ),
        retention_cleanup_batch_size=_integer_environment(
            "EARSHOT_RETENTION_CLEANUP_BATCH_SIZE", 1000
        ),
        analyzer_version=ANALYZER_VERSION,
        behind_tls_proxy=BEHIND_TLS_PROXY,
    ),
    enable_platform_adapter=PLATFORM_ADAPTER_ENABLED,
)
