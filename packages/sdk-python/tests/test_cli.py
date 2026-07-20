from __future__ import annotations

import json

import pytest

from earshot.cli import _build_parser, main
from earshot.codec import encode_incident_json, encode_incident_protobuf
from earshot.contract import ExportPolicy
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.integration


def test_serve_honors_trusted_proxy_environment(monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_BEHIND_TLS_PROXY", "true")

    arguments = _build_parser().parse_args(["serve"])

    assert arguments.behind_tls_proxy is True


def test_serve_reports_the_active_data_path(monkeypatch, tmp_path, capsys) -> None:
    observed = {}
    monkeypatch.setattr("earshot.cli.create_app", lambda **_kwargs: object())
    monkeypatch.setattr("uvicorn.run", lambda _app, **kwargs: observed.update(kwargs))

    assert main(["serve", "--data-dir", str(tmp_path)]) == 0

    assert str(tmp_path.resolve()) in capsys.readouterr().err
    assert observed["host"] == "127.0.0.1"


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


def test_cli_provisions_project_key_and_connector_without_persisting_secrets(
    tmp_path, capsys
) -> None:
    data_dir = tmp_path / "data"

    assert (
        main(
            [
                "project",
                "create",
                "support",
                "--display-name",
                "Support Voice",
                "--data-dir",
                str(data_dir),
            ]
        )
        == 0
    )
    project = json.loads(capsys.readouterr().out)
    assert project["project_id"] == "support"

    assert (
        main(
            [
                "api-key",
                "issue",
                "--project",
                "support",
                "--label",
                "production-ingest",
                "--data-dir",
                str(data_dir),
            ]
        )
        == 0
    )
    issued = json.loads(capsys.readouterr().out)
    assert issued["credential"].startswith("earshot_sk_")
    assert issued["warning"] == "credential is shown once; store it securely"

    assert (
        main(
            [
                "connector",
                "create",
                "--project",
                "support",
                "--provider",
                "elevenlabs",
                "--secret-env",
                "ELEVENLABS_WEBHOOK_SECRET",
                "--data-dir",
                str(data_dir),
            ]
        )
        == 0
    )
    connector = json.loads(capsys.readouterr().out)
    assert connector["hook_path"] == (
        f"/hooks/v1/connectors/{connector['endpoint_id']}"
    )
    assert connector["secret_ref"] == "env:ELEVENLABS_WEBHOOK_SECRET"
    assert issued["credential"].encode() not in (data_dir / "earshot.sqlite3").read_bytes()

    assert (
        main(
            [
                "api-key",
                "revoke",
                "--project",
                "support",
                "--key-id",
                issued["key_id"],
                "--data-dir",
                str(data_dir),
            ]
        )
        == 0
    )
    revoked = json.loads(capsys.readouterr().out)
    assert revoked == {
        "key_id": issued["key_id"],
        "project_id": "support",
        "revoked": True,
    }
