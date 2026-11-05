"""Accept an ambiguous npm publish only when registry bytes and provenance match."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

PUBLISH_PREDICATE = "https://github.com/npm/attestation/tree/main/specs/publish/v0.1"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
STATEMENT_TYPES = {
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
}
NPM_REGISTRY = "https://registry.npmjs.org"


class NpmPublishVerificationError(ValueError):
    """Raised when the npm registry result is not this release's publication."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _statement(bundle: Any, *, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        envelope = _dict(_dict(bundle).get("dsseEnvelope"))
        payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise NpmPublishVerificationError(
            f"npm {label} attestation payload is malformed"
        ) from error
    if not isinstance(payload, dict):
        raise NpmPublishVerificationError(f"npm {label} attestation payload is malformed")
    return payload, envelope


def verify_npm_publish(
    *,
    artifact: Path,
    audit_report: Path,
    package: str,
    version: str,
    repository: str,
    workflow_path: str,
    ref: str,
    commit: str,
    run_id: str,
    run_attempt: int,
) -> None:
    """Require npm's verified signature report to describe this exact release.

    ``npm audit signatures --json --include-attestations`` performs the Sigstore
    verification. This check then requires both npm attestation types and matches
    the signed SLSA statement to the tarball and this workflow run.
    """

    report = json.loads(audit_report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise NpmPublishVerificationError("npm audit signatures report is not an object")
    if report.get("invalid") or report.get("missing"):
        raise NpmPublishVerificationError("npm reported invalid or missing registry signatures")
    verified = report.get("verified")
    if not isinstance(verified, list):
        raise NpmPublishVerificationError("npm did not return verified package signatures")

    matches = [
        item
        for item in verified
        if isinstance(item, dict) and item.get("name") == package and item.get("version") == version
    ]
    if len(matches) != 1:
        raise NpmPublishVerificationError("npm did not verify exactly one matching package version")

    bundles = _dict(matches[0]).get("attestationBundles")
    if not isinstance(bundles, list):
        raise NpmPublishVerificationError("npm did not return verified attestation bundles")
    publish_bundles = [
        bundle
        for bundle in bundles
        if isinstance(bundle, dict) and bundle.get("predicateType") == PUBLISH_PREDICATE
    ]
    if len(publish_bundles) != 1:
        raise NpmPublishVerificationError("npm publish attestation is missing or ambiguous")

    slsa_bundles = [
        bundle
        for bundle in bundles
        if isinstance(bundle, dict) and bundle.get("predicateType") == SLSA_PREDICATE
    ]
    if len(slsa_bundles) != 1:
        raise NpmPublishVerificationError(
            "npm returned an ambiguous set of SLSA provenance bundles"
        )
    actual_digest = hashlib.sha512(artifact.read_bytes()).hexdigest()
    expected_purl = f"pkg:npm/{quote(package, safe='/')}@{version}"

    publish_bundle = _dict(publish_bundles[0].get("bundle"))
    publish_statement, publish_envelope = _statement(publish_bundle, label="publish")
    publish_signatures = publish_envelope.get("signatures")
    first_publish_signature = (
        _dict(publish_signatures[0])
        if isinstance(publish_signatures, list) and publish_signatures
        else {}
    )
    if not first_publish_signature.get("keyid") or not _dict(
        _dict(publish_bundle.get("verificationMaterial")).get("publicKey")
    ):
        raise NpmPublishVerificationError(
            "npm publish attestation is not signed with an npm registry key"
        )
    if (
        publish_statement.get("_type") not in STATEMENT_TYPES
        or publish_statement.get("predicateType") != PUBLISH_PREDICATE
    ):
        raise NpmPublishVerificationError(
            "npm publish attestation payload has an unexpected predicate type"
        )
    publish_subjects = publish_statement.get("subject")
    publish_subject = (
        _dict(publish_subjects[0])
        if isinstance(publish_subjects, list) and len(publish_subjects) == 1
        else {}
    )
    publish_digest = _dict(publish_subject.get("digest")).get("sha512")
    publish_predicate = _dict(publish_statement.get("predicate"))
    if publish_subject.get("name") != expected_purl or publish_digest != actual_digest:
        raise NpmPublishVerificationError(
            "npm publish attestation integrity does not match the retained tarball"
        )
    if (
        publish_predicate.get("name") != package
        or publish_predicate.get("version") != version
        or publish_predicate.get("registry") != NPM_REGISTRY
    ):
        raise NpmPublishVerificationError("npm publish attestation identity is unexpected")

    payload, _ = _statement(_dict(slsa_bundles[0].get("bundle")), label="SLSA provenance")

    if payload.get("_type") not in STATEMENT_TYPES:
        raise NpmPublishVerificationError("npm SLSA provenance has an unexpected statement type")
    subjects = payload.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise NpmPublishVerificationError("npm SLSA provenance has an unexpected subject list")
    subject = _dict(subjects[0])
    subject_digest = _dict(subject.get("digest")).get("sha512")
    if subject.get("name") != expected_purl or subject_digest != actual_digest:
        raise NpmPublishVerificationError(
            "npm SLSA provenance integrity does not match the retained tarball"
        )

    if payload.get("predicateType") != SLSA_PREDICATE:
        raise NpmPublishVerificationError("npm provenance has an unexpected predicate type")
    predicate = _dict(payload.get("predicate"))
    build_definition = _dict(predicate.get("buildDefinition"))
    if (
        build_definition.get("buildType")
        != "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
    ):
        raise NpmPublishVerificationError("npm provenance is not from a GitHub Actions workflow")
    external = _dict(build_definition.get("externalParameters"))
    workflow = _dict(external.get("workflow"))
    expected_repository_url = f"https://github.com/{repository}"
    if (
        workflow.get("repository") != expected_repository_url
        or workflow.get("path") != workflow_path
        or workflow.get("ref") != ref
    ):
        raise NpmPublishVerificationError(
            "npm provenance workflow repository, path, or ref does not match"
        )

    expected_source = {
        "uri": f"git+{expected_repository_url}@{ref}",
        "gitCommit": commit,
    }
    resolved_dependencies = build_definition.get("resolvedDependencies")
    matching_sources = [
        dependency
        for dependency in (resolved_dependencies if isinstance(resolved_dependencies, list) else [])
        if isinstance(dependency, dict)
        and dependency.get("uri") == expected_source["uri"]
        and _dict(dependency.get("digest")).get("gitCommit") == expected_source["gitCommit"]
    ]
    if len(matching_sources) != 1:
        raise NpmPublishVerificationError("npm provenance source commit does not match this tag")

    run_details = _dict(payload.get("predicate"))
    run_details = _dict(run_details.get("runDetails"))
    metadata = _dict(run_details.get("metadata"))
    invocation_id = metadata.get("invocationId")
    prefix = f"{expected_repository_url}/actions/runs/{run_id}/attempts/"
    attempt_text = (
        invocation_id[len(prefix) :]
        if isinstance(invocation_id, str) and invocation_id.startswith(prefix)
        else ""
    )
    if not re.fullmatch(r"[1-9][0-9]*", attempt_text):
        raise NpmPublishVerificationError("npm provenance is not from this workflow run")
    if int(attempt_text) > run_attempt:
        raise NpmPublishVerificationError("npm provenance is from a future workflow attempt")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--audit-report", required=True, type=Path)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    args = parser.parse_args()

    try:
        verify_npm_publish(
            artifact=args.artifact,
            audit_report=args.audit_report,
            package=args.package,
            version=args.version,
            repository=args.repository,
            workflow_path=args.workflow_path,
            ref=args.ref,
            commit=args.commit,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
    except (OSError, json.JSONDecodeError, NpmPublishVerificationError) as error:
        parser.error(str(error))

    print("npm package bytes and provenance match the same workflow run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
