"""Golden verification against REAL captured provider deliveries.

The synthetic connector tests prove our *understanding* of each provider's
contract. This harness proves the *actual* contract: drop a genuine captured
webhook delivery into ``fixtures/connectors/<provider>/`` and this test asserts
that our connector authenticates the real signature and normalizes the real
bytes into a valid, privacy-safe incident. Without captures it skips, so CI
stays green until a real delivery is supplied.

This is the definitive answer to "does the signature format match the live
provider?" — a `DeliveryTrustError` here means our HMAC scheme disagrees with
what the provider actually sent.

Capture format — one JSON file per delivery, e.g.
``fixtures/connectors/retell/call_analyzed_example.json``::

    {
      "provider": "retell",
      "secret_env": "RETELL_WEBHOOK_SECRET",
      "expected_disposition": "applied",
      "headers": [["X-Retell-Signature", "v=...,d=..."], ["Content-Type", "application/json"]],
      "body_base64": "<base64 of the EXACT raw request body bytes>",
      "must_not_appear": ["a phrase from the transcript", "the raw call id"]
    }

``body_base64`` is required: any re-serialization changes the bytes the provider
signed, so only the exact captured bytes can validate a real signature. Signature
skew enforcement is intentionally disabled here (a captured delivery is replayed
later than it was signed); the offline suites cover skew separately. See
``fixtures/connectors/README.md`` for how to capture one per provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from earshot.codec import decode_incident_protobuf
from earshot.connectors import (
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "connectors"
_SECRET_REF = "env:REAL_PROVIDER_SECRET"
# A tolerance wide enough that a delivery captured at any point in the past still
# validates: this test isolates signature-FORMAT correctness from clock skew,
# which the synthetic suites already assert with stale-timestamp cases.
_REPLAY_TOLERANCE_SECONDS = 10**15


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
def test_real_provider_delivery_authenticates_and_normalizes(capture_path, tmp_path) -> None:
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

    store = IncidentStore(tmp_path)
    store.create_project("real", display_name="Real capture")
    endpoint = store.create_connector("real", provider=provider, secret_ref=_SECRET_REF)
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({_SECRET_REF: secret}),
        signature_tolerance_seconds=_REPLAY_TOLERANCE_SECONDS,
    )

    # Raises DeliveryTrustError if our signature scheme disagrees with the provider.
    outcome = ingestion.receive(
        RawProviderDelivery(endpoint_id=endpoint.endpoint_id, headers=headers, body=body)
    )

    expected_disposition = spec.get("expected_disposition", "applied")
    if expected_disposition not in {"applied", "ignored"}:
        pytest.fail(
            f"{capture_path.name}: expected_disposition must be 'applied' or 'ignored'"
        )
    assert outcome.disposition == expected_disposition
    if outcome.disposition != "applied":
        return

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="real")
    bundle = decode_incident_protobuf(canonical)
    assert validate_incident(bundle).ok, f"{capture_path.name}: real delivery failed the contract"
    # A real capture carries genuine PII; confirm it never reaches the canonical bytes.
    for index, sentinel in enumerate(spec.get("must_not_appear", [])):
        if not isinstance(sentinel, str):
            pytest.fail(f"{capture_path.name}: privacy sentinel {index} must be text")
        digest = hashlib.sha256(sentinel.encode()).hexdigest()[:12]
        assert sentinel.encode() not in canonical, (
            f"{capture_path.name}: privacy sentinel {index} ({digest}) leaked"
        )
