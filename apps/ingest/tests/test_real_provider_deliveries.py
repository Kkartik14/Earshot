"""Golden verification against REAL captured provider deliveries.

The connector tests prove our *understanding* of each provider's contract, on
documented payloads (and, for ElevenLabs, a real conversation re-signed with a test
secret). This harness proves the *actual* contract: drop a genuine captured webhook
delivery into ``fixtures/connectors/<provider>/`` and this test asserts that our
connector authenticates the real signature and translates the real bytes into call
evidence that keeps no URL, and no listed sentinel under ``metadata_only``. Without
captures it skips, so CI stays green until a real delivery is supplied.

A ``DeliveryTrustError`` here means our signature scheme disagrees with what the
provider actually sent.

Capture format — one JSON file per delivery, e.g.
``fixtures/connectors/retell/call_analyzed_example.json``::

    {
      "provider": "retell",
      "secret_env": "RETELL_WEBHOOK_SECRET",
      "signed_at_unix_seconds": 1792490400,
      "expected_disposition": "applied",
      "headers": [["X-Retell-Signature", "v=...,d=..."], ["Content-Type", "application/json"]],
      "body_base64": "<base64 of the EXACT raw request body bytes>",
      "must_not_appear": ["a phrase from the transcript", "the raw call id"]
    }

``body_base64`` is required: any re-serialization changes the bytes the provider
signed, so only the exact captured bytes can validate a real signature. The replay
runs at ``signed_at_unix_seconds`` (when the provider signed it), so each provider's
own signature window applies unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from earshot.connectors import (
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from store_support import make_store

pytestmark = pytest.mark.integration

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "connectors"
_SECRET_REF = "env:REAL_PROVIDER_SECRET"


def _captures() -> list[Path]:
    if not _FIXTURE_ROOT.exists():
        return []
    return sorted(path for path in _FIXTURE_ROOT.glob("*/*.json"))


_CAPTURES = _captures()
_PARAMS = _CAPTURES or [
    pytest.param(
        None,
        marks=pytest.mark.skip(
            reason=(
                "no captured real provider deliveries; add one under "
                "apps/ingest/tests/fixtures/connectors/<provider>/ "
                "(see fixtures/connectors/README.md)"
            )
        ),
    )
]


@pytest.mark.parametrize(
    "capture_path",
    _PARAMS,
    ids=lambda path: f"{path.parent.name}/{path.name}" if isinstance(path, Path) else "none",
)
def test_real_provider_delivery_authenticates_and_translates(capture_path, tmp_path) -> None:
    spec = json.loads(capture_path.read_text())
    provider = spec["provider"]
    secret_env = spec.get("secret_env")
    if not isinstance(secret_env, str) or not secret_env:
        pytest.fail(f"{capture_path.name}: captures must name a non-empty 'secret_env'")
    secret = os.environ.get(secret_env)
    if secret is None:
        pytest.skip(f"{capture_path.name}: required secret environment variable is not set")
    if "body_base64" not in spec:
        pytest.fail(
            f"{capture_path.name}: captures must include 'body_base64' (the exact "
            "signed bytes); a re-serialized body cannot validate a real signature"
        )
    body = base64.b64decode(spec["body_base64"])
    headers = tuple((str(name).encode(), str(value).encode()) for name, value in spec["headers"])

    store = make_store(tmp_path)
    store.create_project("real", display_name="Real capture")
    store.set_capture_preset("real", "metadata_only")
    endpoint = store.create_connector("real", provider=provider, secret_ref=_SECRET_REF)
    signed_at = spec.get("signed_at_unix_seconds")
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({_SECRET_REF: secret}),
        now_unix_seconds=(lambda: signed_at) if isinstance(signed_at, int) else None,
    )

    # Raises DeliveryTrustError if our signature scheme disagrees with the provider.
    outcome = ingestion.receive(
        RawProviderDelivery(endpoint_id=endpoint.endpoint_id, headers=headers, body=body)
    )

    expected_disposition = spec.get("expected_disposition", "applied")
    if expected_disposition not in {"applied", "ignored"}:
        pytest.fail(f"{capture_path.name}: expected_disposition must be 'applied' or 'ignored'")
    assert outcome.disposition == expected_disposition
    if outcome.disposition != "applied":
        return
    assert outcome.call_id is not None

    with store._connect() as connection:
        stored = b"".join(
            bytes(row["payload"])
            for row in connection.execute("SELECT payload FROM spans").fetchall()
        )
    assert stored, f"{capture_path.name}: the delivery stored no evidence"
    assert b"https://" not in stored, f"{capture_path.name}: a URL reached a span"
    # A real capture carries genuine PII; under metadata_only none of it is stored.
    for index, sentinel in enumerate(spec.get("must_not_appear", [])):
        if not isinstance(sentinel, str):
            pytest.fail(f"{capture_path.name}: privacy sentinel {index} must be text")
        digest = hashlib.sha256(sentinel.encode()).hexdigest()[:12]
        assert sentinel.encode() not in stored, (
            f"{capture_path.name}: privacy sentinel {index} ({digest}) leaked"
        )
