"""Conformance every connector's evidence meets, on each provider's final delivery.

- Every attribute a connector span carries is registered: the ``earshot.connector.*``
  vocabulary and the receiver's keys in ``semconv/earshot.yaml``, or the
  OpenTelemetry GenAI agent keys.
- No URL, and so no signed query string, reaches a stored span under any preset:
  recording URLs go to the fetch job only.
- Under ``metadata_only`` no transcript text, summary or provider call ID is stored.
- The same delivery to two stores stores the same span bytes (the evidence is a
  pure function of the payload).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import test_elevenlabs_connector as elevenlabs
import test_retell_connector as retell
import test_ringg_connector as ringg
import test_vapi_connector as vapi
from connector_support import ROOT, connector_world
from earshot.call_capture import attribute_values

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class FinalCase:
    provider: str
    payload: Any
    # What must never be stored under metadata_only.
    content: tuple[str, ...]


CASES = (
    FinalCase(
        "elevenlabs",
        elevenlabs._transcription,
        ("table for two", "quiet near the window", elevenlabs.CONVERSATION_ID),
    ),
    FinalCase("vapi", vapi._report, ("Green Valley", "moved a clinic visit", vapi.CALL_ID)),
    FinalCase(
        "retell",
        lambda: retell._call("call_analyzed"),
        ("move my visit", "asked to move", retell.CALL_ID),
    ),
    FinalCase("ringg", ringg._final, ("payment reminder", "confirmed the payment", ringg.CALL_ID)),
)


def _registered() -> set[str]:
    registry = yaml.safe_load((ROOT / "semconv" / "earshot.yaml").read_text())
    [group] = [group for group in registry["groups"] if group["id"] == "registry.earshot"]
    return {attribute["id"] for attribute in group["attributes"]} | {
        "gen_ai.agent.id",
        "gen_ai.agent.name",
    }


def _stored(path: Path, case: FinalCase, *, preset: str = "full"):
    world = connector_world(path, case.provider, preset=preset)
    assert world.deliver(case.payload()).disposition == "applied"
    payloads = world.rows("SELECT payload, text_json FROM spans ORDER BY trace_id, span_id")
    resources = world.rows("SELECT payload FROM otlp_resources")
    return world, payloads, resources


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
def test_connector_evidence_conforms(tmp_path, case) -> None:
    world, payloads, resources = _stored(tmp_path / "first", case)
    _, again, _ = _stored(tmp_path / "second", case)
    registered = _registered()

    keys = {
        key
        for span in world.spans()
        for key in attribute_values(span.span.attributes)
        if not key.startswith("earshot.session.")
    }
    assert keys - registered == set()
    stored = b"".join(bytes(payload) for payload, _ in payloads) + b"".join(
        bytes(resource) for (resource,) in resources
    )
    assert b"https://" not in stored and b"Signature" not in stored
    assert [bytes(payload) for payload, _ in payloads] == [bytes(payload) for payload, _ in again]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
def test_metadata_only_stores_no_content_of_any_connector(tmp_path, case) -> None:
    world, payloads, resources = _stored(tmp_path, case, preset="metadata_only")

    stored = b"".join(
        bytes(payload) + (text or "").encode() for payload, text in payloads
    ) + b"".join(bytes(resource) for (resource,) in resources)
    for value in case.content:
        assert value.encode() not in stored, value
    assert world.rows("SELECT count(*) FROM recordings") == [(0,)]
