from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_npm_publish.py"
PACKAGE = "earshot-observability"
VERSION = "0.4.0"
REPOSITORY = "Kkartik14/Earshot"
COMMIT = "a" * 40
RUN_ID = "123456789"


def _payload(artifact: bytes, **overrides: Any) -> dict[str, Any]:
    repo_url = f"https://github.com/{REPOSITORY}"
    ref = f"refs/tags/v{VERSION}"
    values: dict[str, Any] = {
        "repository": repo_url,
        "workflow_path": ".github/workflows/release.yml",
        "ref": ref,
        "commit": COMMIT,
        "invocation_id": f"{repo_url}/actions/runs/{RUN_ID}/attempts/1",
        "build_type": "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1",
    }
    values.update(overrides)
    digest = hashlib.sha512(artifact).hexdigest()
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": f"pkg:npm/{PACKAGE}@{VERSION}",
                "digest": {"sha512": digest},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": values["build_type"],
                "externalParameters": {
                    "workflow": {
                        "repository": values["repository"],
                        "path": values["workflow_path"],
                        "ref": values["ref"],
                    }
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+{repo_url}@{values['ref']}",
                        "digest": {"gitCommit": values["commit"]},
                    }
                ],
            },
            "runDetails": {"metadata": {"invocationId": values["invocation_id"]}},
        },
    }


def _report(payload: dict[str, Any], *, include_publish: bool = True) -> dict[str, Any]:
    encoded_payload = base64.b64encode(json.dumps(payload).encode()).decode()
    bundles = [
        {
            "predicateType": "https://slsa.dev/provenance/v1",
            "bundle": {"dsseEnvelope": {"payload": encoded_payload}},
        }
    ]
    if include_publish:
        bundles.append(
            {
                "predicateType": "https://github.com/npm/attestation/tree/main/specs/publish/v0.1",
                "bundle": {"dsseEnvelope": {"payload": "e30="}},
            }
        )
    return {
        "invalid": [],
        "missing": [],
        "verified": [
            {
                "name": PACKAGE,
                "version": VERSION,
                "attestationBundles": bundles,
            }
        ],
    }


def _run_verifier(
    tmp_path: Path, artifact: bytes, report: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    artifact_path = tmp_path / "node-package.tgz"
    report_path = tmp_path / "audit-signatures.json"
    artifact_path.write_bytes(artifact)
    report_path.write_text(json.dumps(report))
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact",
            str(artifact_path),
            "--audit-report",
            str(report_path),
            "--package",
            PACKAGE,
            "--version",
            VERSION,
            "--repository",
            REPOSITORY,
            "--workflow-path",
            ".github/workflows/release.yml",
            "--ref",
            f"refs/tags/v{VERSION}",
            "--commit",
            COMMIT,
            "--run-id",
            RUN_ID,
            "--run-attempt",
            "2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_npm_retry_accepts_same_artifact_from_an_earlier_attempt_of_this_run(
    tmp_path: Path,
) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact))

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 0
    assert "same workflow run" in result.stdout


def test_npm_retry_rejects_a_different_tarball(tmp_path: Path) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact))

    result = _run_verifier(tmp_path, b"different package bytes", report)

    assert result.returncode == 2
    assert "integrity" in result.stderr


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("repository", "https://github.com/untrusted/other"),
        ("workflow_path", ".github/workflows/unrelated.yml"),
        ("ref", "refs/heads/main"),
        ("commit", "b" * 40),
        ("invocation_id", "https://github.com/Kkartik14/Earshot/actions/runs/987654321/attempts/1"),
    ],
)
def test_npm_retry_rejects_provenance_from_another_release_identity(
    tmp_path: Path, field: str, wrong_value: str
) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact, **{field: wrong_value}))

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 2
    assert "provenance" in result.stderr


def test_npm_retry_requires_both_publish_and_slsa_attestations(tmp_path: Path) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact), include_publish=False)

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 2
    assert "publish attestation" in result.stderr


def test_npm_retry_rejects_invalid_or_missing_registry_signatures(tmp_path: Path) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact))
    report["invalid"] = [{"name": PACKAGE, "version": VERSION}]

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 2
    assert "invalid or missing registry signatures" in result.stderr


def test_npm_retry_rejects_a_provenance_from_a_future_attempt(tmp_path: Path) -> None:
    artifact = b"the immutable npm tarball"
    repo_url = f"https://github.com/{REPOSITORY}"
    report = _report(
        _payload(
            artifact,
            invocation_id=f"{repo_url}/actions/runs/{RUN_ID}/attempts/3",
        )
    )

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 2
    assert "future workflow attempt" in result.stderr


def test_npm_retry_rejects_a_non_github_workflow_provenance(tmp_path: Path) -> None:
    artifact = b"the immutable npm tarball"
    report = _report(_payload(artifact, build_type="https://example.test/build/v1"))

    result = _run_verifier(tmp_path, artifact, report)

    assert result.returncode == 2
    assert "not from a GitHub Actions workflow" in result.stderr
