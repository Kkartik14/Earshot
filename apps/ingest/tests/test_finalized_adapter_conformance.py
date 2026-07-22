"""Conformance for finalized Provider Deliveries using sanitized synthetic fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import test_elevenlabs_connector as elevenlabs
import test_retell_connector as retell
import test_ringg_connector as ringg
import test_vapi_connector as vapi
from adapter_conformance import assert_canonical_payload_conforms
from earshot.connectors import HostedProviderIngestion, MappingSecretResolver
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

IDENTITY_KEY = b"c" * 32


@dataclass(frozen=True)
class FinalizedCase:
    provider: str
    secret_ref: str
    secret: str
    payload: object
    delivery: object
    forbidden_values: tuple[str, ...]
    now_unix_seconds: int | None = None


CASES = (
    FinalizedCase(
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_WEBHOOK_SECRET",
        secret=elevenlabs.WEBHOOK_SECRET,
        payload=elevenlabs._payload,
        delivery=elevenlabs._delivery,
        forbidden_values=(
            elevenlabs.TRANSCRIPT_SENTINEL,
            "private-agent-response",
            "private-dynamic-variable",
            "conversation-sensitive-456",
        ),
        now_unix_seconds=1_739_537_300,
    ),
    FinalizedCase(
        provider="vapi",
        secret_ref="env:VAPI_SERVER_SECRET",
        secret=vapi.SECRET,
        payload=vapi._payload,
        delivery=vapi._delivery,
        forbidden_values=(vapi.PRIVATE_TEXT, "call-sensitive-vapi-123"),
    ),
    FinalizedCase(
        provider="retell",
        secret_ref="env:RETELL_WEBHOOK_API_KEY",
        secret=retell.SECRET,
        payload=retell._payload,
        delivery=retell._delivery,
        forbidden_values=(retell.PRIVATE_TEXT, "call-sensitive-retell-123"),
        now_unix_seconds=retell.NOW_MS // 1_000,
    ),
    FinalizedCase(
        provider="ringg",
        secret_ref="env:RINGG_WEBHOOK_SECRET",
        secret=ringg.SECRET,
        payload=ringg._payload,
        delivery=ringg._delivery,
        forbidden_values=(
            ringg.PRIVATE_TEXT,
            ringg.PRIVATE_PHONE,
            ringg.PRIVATE_URL,
            ringg.CALL_ID,
            ringg.CALL_SID,
        ),
    ),
)


def _capture(case: FinalizedCase, data_dir: Path) -> bytes:
    data_dir.mkdir()
    (data_dir / "instance-correlation.key").write_bytes(IDENTITY_KEY)
    store = IncidentStore(data_dir)
    store.create_project("support", display_name="Support")
    endpoint = store.create_connector(
        "support",
        provider=case.provider,
        secret_ref=case.secret_ref,
        endpoint_id=f"connector_conformance_{case.provider}",
    )
    options = {}
    if case.now_unix_seconds is not None:
        options["now_unix_seconds"] = lambda: case.now_unix_seconds
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({case.secret_ref: case.secret}),
        **options,
    )
    body = case.payload()
    delivery = case.delivery(endpoint.endpoint_id, body)

    outcome = ingestion.receive(delivery)
    replay = ingestion.receive(delivery)

    assert outcome.disposition == "applied"
    assert replay.disposition == "replayed"
    assert replay.bundle_id == outcome.bundle_id
    assert outcome.bundle_id is not None
    canonical = store.get_artifact(outcome.bundle_id, project_id="support")[1]
    store.close()
    return canonical


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
def test_finalized_delivery_sanitized_synthetic_capture_meets_shared_conformance(
    case: FinalizedCase,
    tmp_path,
) -> None:
    first = _capture(case, tmp_path / "first")
    second = _capture(case, tmp_path / "second")

    assert_canonical_payload_conforms(
        first,
        deterministic_peer=second,
        forbidden_values=case.forbidden_values,
    )
