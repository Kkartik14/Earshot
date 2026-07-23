from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.codec import decode_incident_json
from earshot.validation import validate_incident
from earshot.versions import (
    LIVEKIT_ADAPTER_VERSION,
    PIPECAT_ADAPTER_VERSION,
    PIPELINE_ADAPTER_VERSION,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
CAPTURED = ROOT / "fixtures" / "captured"
SHA256 = re.compile(r"[0-9a-f]{64}")
EXPECTED_ADAPTER_VERSION = {
    "cartesia": PIPELINE_ADAPTER_VERSION,
    "deepgram": PIPELINE_ADAPTER_VERSION,
    "livekit": LIVEKIT_ADAPTER_VERSION,
    "pipecat": PIPECAT_ADAPTER_VERSION,
    "sarvam": PIPELINE_ADAPTER_VERSION,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest() -> dict[str, object]:
    return json.loads((CAPTURED / "manifest.json").read_text())


def test_retained_real_captures_validate_and_remain_metadata_only() -> None:
    manifest = _manifest()
    entries = manifest["artifacts"]
    assert {entry["surface"] for entry in entries} == {
        "cartesia",
        "deepgram",
        "livekit",
        "pipecat",
        "sarvam",
    }

    for entry in entries:
        path = CAPTURED / entry["file"]
        bundle = decode_incident_json(path.read_bytes(), validate=False)
        assert validate_incident(bundle).ok, path
        assert bundle.profile.operations, path
        assert not bundle.raw_otlp_chunks, path
        assert not bundle.profile.media_refs, path
        assert entry["source_kind"] == "retained_real_capture"
        assert entry["captured_on"] == "2026-07-23"
        assert entry["migration"] == "none_current_contract"
        assert entry["adapter_version"] == EXPECTED_ADAPTER_VERSION[entry["surface"]]
        assert bundle.profile.manifest.adapters[0].version == entry["adapter_version"]
        assert entry["artifact_sha256"] == _sha256(path)
        assert SHA256.fullmatch(entry["source_sha256"])
        assert entry["source_sha256"] != entry["artifact_sha256"]
        driver = ROOT / entry["capture_driver"]
        redactor = ROOT / entry["redaction_tool"]
        assert entry["capture_driver_sha256"] == _sha256(driver)
        assert entry["redaction_tool_sha256"] == _sha256(redactor)
        assert all(
            policy.capture_class == "metadata" or not policy.captured
            for policy in bundle.profile.privacy.capture_classes
        ), path


def test_cli_validates_every_retained_real_capture(capsys: pytest.CaptureFixture[str]) -> None:
    for entry in _manifest()["artifacts"]:
        path = CAPTURED / entry["file"]
        assert main(["validate", str(path)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["valid"] is True
        assert result["issues"] == []
