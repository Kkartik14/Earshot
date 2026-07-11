from __future__ import annotations

import json

import pytest

from earshot.cli import main
from earshot.codec import encode_incident_json, encode_incident_protobuf
from earshot.contract import ExportPolicy
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.integration


def test_validate_json_and_protobuf_files(tmp_path, valid_bundle, capsys) -> None:
    json_path = tmp_path / "incident.json"
    protobuf_path = tmp_path / "incident.pb"
    json_path.write_bytes(encode_incident_json(valid_bundle))
    protobuf_path.write_bytes(encode_incident_protobuf(valid_bundle))
    for path in (json_path, protobuf_path):
        assert main(["validate", str(path)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["valid"] is True
        assert output["bundle_id"] == "bundle-1"
        assert len(output["canonical_sha256"]) == 64


def test_cli_ingest_list_show_and_purge_workflow(tmp_path, valid_bundle, capsys) -> None:
    artifact = tmp_path / "incident.pb"
    data_dir = tmp_path / "data"
    artifact.write_bytes(encode_incident_protobuf(valid_bundle))

    assert main(["ingest", str(artifact), "--data-dir", str(data_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    assert main(["ingest", str(artifact), "--data-dir", str(data_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False

    assert main(["list", "--data-dir", str(data_dir)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["bundle_id"] for item in listed["items"]] == ["bundle-1"]

    assert main(["show", "bundle-1", "--data-dir", str(data_dir)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["profile"]["manifest"]["bundle_id"] == "bundle-1"

    assert main(["purge", "bundle-1", "--data-dir", str(data_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["purged"] is True
    assert main(["show", "bundle-1", "--data-dir", str(data_dir)]) == 2
    assert "IncidentPurgedError" in capsys.readouterr().err


def test_cli_error_never_reflects_sensitive_invalid_input(tmp_path, valid_bundle, capsys) -> None:
    value = json.loads(encode_incident_json(valid_bundle))
    value["profile"]["manifest"]["session_id"] = SECRET_SENTINEL
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value))
    assert main(["validate", str(path)]) == 1
    captured = capsys.readouterr()
    assert SECRET_SENTINEL not in captured.out
    assert SECRET_SENTINEL not in captured.err
    assert json.loads(captured.out)["valid"] is False


def test_cli_show_enforces_destination_export_policy(tmp_path, valid_bundle, capsys) -> None:
    policies = list(valid_bundle.profile.privacy.capture_classes)
    policies[0] = policies[0].model_copy(
        update={"export": ExportPolicy(allowed=False, policy_id="deny-export")}
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": tuple(policies)})
    bundle = valid_bundle.model_copy(
        update={"profile": valid_bundle.profile.model_copy(update={"privacy": privacy})}
    )
    artifact = tmp_path / "restricted.pb"
    data_dir = tmp_path / "data"
    artifact.write_bytes(encode_incident_protobuf(bundle))
    assert main(["ingest", str(artifact), "--data-dir", str(data_dir)]) == 0
    capsys.readouterr()
    assert main(["show", "bundle-1", "--data-dir", str(data_dir)]) == 2
    captured = capsys.readouterr()
    assert "ExportPolicyError" in captured.err
    assert SECRET_SENTINEL not in captured.err
