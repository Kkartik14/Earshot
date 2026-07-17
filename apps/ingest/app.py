"""ASGI entry point for ``uvicorn apps.ingest.app:app``."""

from __future__ import annotations

import os
from pathlib import Path

from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.api import ApiConfig, create_app


def _integer_environment(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value is not None else default


HOST = os.environ.get("EARSHOT_HOST", "127.0.0.1")
TOKEN = os.environ.get("EARSHOT_TOKEN")
DATA_DIR = Path(os.environ.get("EARSHOT_DATA_DIR", ".earshot"))
BEHIND_TLS_PROXY = os.environ.get("EARSHOT_BEHIND_TLS_PROXY", "").lower() in {
    "1",
    "true",
    "yes",
}

app = create_app(
    data_dir=DATA_DIR,
    analyzer=analyze_incident,
    config=ApiConfig(
        host=HOST,
        token=TOKEN,
        max_body_bytes=_integer_environment("EARSHOT_MAX_BODY_BYTES", 16 * 1024 * 1024),
        max_connector_body_bytes=_integer_environment(
            "EARSHOT_MAX_CONNECTOR_BODY_BYTES", 2 * 1024 * 1024
        ),
        max_connector_deliveries_per_minute=_integer_environment(
            "EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE", 120
        ),
        analyzer_version=ANALYZER_VERSION,
        behind_tls_proxy=BEHIND_TLS_PROXY,
    ),
)
