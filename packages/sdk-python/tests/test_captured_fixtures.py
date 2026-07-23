from __future__ import annotations

import json
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.codec import decode_incident_json
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
CAPTURED = ROOT / "fixtures" / "captured"


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
