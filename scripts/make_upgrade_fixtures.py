#!/usr/bin/env python3
"""Generate ``fixtures/upgrade``: catalogs written by historical Earshot releases.

Each release's own code is extracted from git with ``git archive`` (read-only) and
run in a subprocess whose ``PYTHONPATH`` is that extracted tree, on this
interpreter's installed dependencies. Through the release's public API it

- ingests that release's canonical fixtures (``fixtures/valid/*.json``) into the
  default project and its fault fixtures (``fixtures/faults/*.incident.json``)
  into a new project, caching each incident's analysis as ``GET …/analysis`` does;
- issues an API key, and a second one that it revokes;
- creates a connector, completes one delivery bound to an incident, and records
  an external identity; links an external reference where the release has them;
- purges one incident.

The catalog file, its CAS objects and the instance correlation key are copied to
``fixtures/upgrade/<release>/``. ``manifest.json`` records the commit, schema
version, file digests, per-table row counts, every live incident's artifact digest,
the CAS objects, and the identifiers and credentials the release issued. The
credentials and the correlation key are random values minted for these throwaway
catalogs; they open nothing else.

The fixtures are generated once and committed: CI checkouts are shallow and have
no history to extract. Re-running refuses to overwrite them without ``--force``.

``--open-with RELEASE --data-dir DIR`` opens ``DIR`` with that release's code
instead and prints ``{"schema_version": N, "opened": true}`` or
``{"schema_version": N, "refused": "<StorageError message>"}``. It exits 4 when
the release's commit is not in this clone.

Usage::

    .venv/bin/python scripts/make_upgrade_fixtures.py [--force] [--output DIR]
    .venv/bin/python scripts/make_upgrade_fixtures.py --open-with 3c2f0b3 --data-dir DIR
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "fixtures" / "upgrade"
SOURCE_PATHS = ("packages/sdk-python/src", "fixtures/valid", "fixtures/faults")
FIXTURE_FILES = ("earshot.sqlite3", "instance-correlation.key")
EXIT_REVISION_UNAVAILABLE = 4

PROJECT_ID = "upgrade-fixture"
CONNECTOR_ENDPOINT_ID = "upgrade_fixture_connector"
CONNECTOR_PROVIDER = "vapi"
PURGED_SOURCE = "fixtures/valid/minimal.json"
IDENTITY_NAMESPACE, IDENTITY_VALUE = "call_id", "upgrade-fixture-call"


@dataclass(frozen=True)
class Release:
    name: str  # the fixture directory and manifest key
    revision: str  # what ``git rev-parse`` resolves
    schema_version: int  # the release's ``_SCHEMA_VERSION``


RELEASES = (
    Release("v0.1.0", "v0.1.0", 10),
    Release("553cecf", "553cecf", 17),
    Release("86c4c9c", "86c4c9c", 18),
    Release("3c2f0b3", "3c2f0b3", 19),
)


# --- Parent: extract a release and run it ---------------------------------------------


def _commit(revision: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _extract(commit: str, destination: Path, paths: tuple[str, ...]) -> None:
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", commit, *paths],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")


def _run_release(release: Release, source: Path, arguments: list[str]) -> dict[str, Any]:
    """Run this script's driver under the release's code; return its JSON result."""

    # Nothing of this tree's configuration reaches the release: no EARSHOT_*
    # settings, no import path, and no coverage hooks (pytest-cov and coverage
    # follow subprocesses through these variables; old code is not this tree's).
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("EARSHOT_", "COV_CORE_", "COVERAGE_")) and key != "PYTHONPATH"
    }
    environment["PYTHONPATH"] = str(source / "packages" / "sdk-python" / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-P",  # never put this script's directory on sys.path
            str(Path(__file__).resolve()),
            "--driver",
            *arguments,
            "--source",
            str(source),
            "--schema-version",
            str(release.schema_version),
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"{release.name}: the release's code failed\n{completed.stderr}")
    return json.loads(completed.stdout)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _describe(release: Release, directory: Path) -> dict[str, Any]:
    """What the release left in ``directory``: file digests, rows, CAS objects."""

    database = directory / "earshot.sqlite3"
    # immutable: read without locks, and without creating -shm/-wal files beside it.
    uri = database.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        table_rows = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    if user_version != release.schema_version:
        raise SystemExit(f"{release.name}: wrote user_version {user_version}")
    objects = {}
    for path in sorted((directory / "objects" / "sha256").glob("*/*")):
        digest = path.parent.name + path.name
        if _sha256_file(path) != digest:
            raise SystemExit(f"{release.name}: CAS object {digest} does not match its name")
        objects[digest] = path.stat().st_size
    return {
        "files": {
            name: {
                "sha256": _sha256_file(directory / name),
                "size": (directory / name).stat().st_size,
            }
            for name in FIXTURE_FILES
        },
        "table_rows": table_rows,
        "objects": objects,
    }


def generate(output: Path, *, force: bool) -> None:
    if output.exists() and any(output.iterdir()):
        if not force:
            raise SystemExit(
                f"{output} already holds fixtures; they are generated once "
                "(pass --force to replace them)"
            )
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    fixtures = {}
    for release in RELEASES:
        commit = _commit(release.revision)
        if commit is None:
            raise SystemExit(f"{release.revision} is not in this clone (fetch the full history)")
        with tempfile.TemporaryDirectory(prefix=f"earshot-upgrade-{release.name}-") as work:
            source, data_dir = Path(work) / "source", Path(work) / "data"
            _extract(commit, source, SOURCE_PATHS)
            written = _run_release(release, source, ["generate", "--data-dir", str(data_dir)])
            if (data_dir / "earshot.sqlite3-wal").exists():
                raise SystemExit(f"{release.name}: the catalog was left with a WAL file")
            target = output / release.name
            target.mkdir()
            for name in FIXTURE_FILES:
                shutil.copyfile(data_dir / name, target / name)
            shutil.copytree(data_dir / "objects", target / "objects")
        fixtures[release.name] = {
            "revision": release.revision,
            "commit": commit,
            "schema_version": release.schema_version,
            "directory": release.name,
            **written,
            **_describe(release, target),
        }
        print(f"{release.name}: schema v{release.schema_version}, commit {commit[:12]}")
    manifest = {
        "generator": {
            "command": ".venv/bin/python scripts/make_upgrade_fixtures.py",
            "python": platform.python_version(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("protobuf", "pydantic", "rfc8785")
            },
        },
        "fixtures": fixtures,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def open_with(name: str, data_dir: Path) -> int:
    release = next((release for release in RELEASES if release.name == name), None)
    if release is None:
        raise SystemExit(f"unknown release {name!r}")
    commit = _commit(release.revision)
    if commit is None:
        print(
            f"{release.revision} is not in this clone (CI checkouts are shallow)", file=sys.stderr
        )
        return EXIT_REVISION_UNAVAILABLE
    with tempfile.TemporaryDirectory(prefix=f"earshot-release-{release.name}-") as work:
        _extract(commit, Path(work), ("packages/sdk-python/src",))
        outcome = _run_release(release, Path(work), ["open", "--data-dir", str(data_dir)])
    print(json.dumps(outcome, sort_keys=True))
    return 0


# --- Driver: runs under the release's code --------------------------------------------


def _release_storage(source: Path, schema_version: int) -> Any:
    """Import the release's storage module, refusing any other ``earshot``."""

    import earshot
    from earshot import storage

    package = Path(earshot.__file__).resolve()
    if not package.is_relative_to(source.resolve()):
        raise SystemExit(f"imported earshot from {package}, not the extracted release")
    if schema_version != storage._SCHEMA_VERSION:
        raise SystemExit(f"the release has schema v{storage._SCHEMA_VERSION}, not {schema_version}")
    return storage


def _drive_generate(data_dir: Path, source: Path, schema_version: int) -> dict[str, Any]:
    storage = _release_storage(source, schema_version)
    from earshot.analysis import ANALYZER_VERSION, analyze_incident
    from earshot.codec import decode_incident_json, encode_incident_protobuf

    sources = [
        (path, storage.DEFAULT_PROJECT_ID) for path in sorted(source.glob("fixtures/valid/*.json"))
    ] + [(path, PROJECT_ID) for path in sorted(source.glob("fixtures/faults/*.incident.json"))]
    store = storage.IncidentStore(data_dir)
    try:
        project = store.create_project(PROJECT_ID, display_name="Upgrade fixture")
        active = store.issue_api_key(PROJECT_ID, label="upgrade fixture")
        revoked = store.issue_api_key(PROJECT_ID, label="upgrade fixture (revoked)")
        store.revoke_api_key(PROJECT_ID, revoked.key_id)
        connector = store.create_connector(
            PROJECT_ID,
            provider=CONNECTOR_PROVIDER,
            secret_ref="env:EARSHOT_UPGRADE_FIXTURE_SECRET",
            endpoint_id=CONNECTOR_ENDPOINT_ID,
        )
        incidents = []
        for path, project_id in sources:
            bundle = decode_incident_json(path.read_bytes())
            record = store.ingest(
                bundle, encode_incident_protobuf(bundle), project_id=project_id
            ).record
            analysis = analyze_incident(
                bundle, input_sha256=record.digest, generated_at_unix_nano=time.time_ns()
            )
            store.put_analysis(record.bundle_id, ANALYZER_VERSION, analysis, project_id=project_id)
            incidents.append(
                {
                    "bundle_id": record.bundle_id,
                    "project_id": project_id,
                    "digest": record.digest,
                    "source": path.relative_to(source).as_posix(),
                }
            )

        linked = next(incident for incident in incidents if incident["project_id"] == PROJECT_ID)
        claim = store.claim_delivery(
            connector,
            delivery_key_hmac=store.fingerprint("delivery", "upgrade-fixture-delivery"),
            body_sha256=hashlib.sha256(b"upgrade fixture delivery").hexdigest(),
            event_type="end-of-call-report",
            now_unix_nano=time.time_ns(),
        )
        store.complete_delivery(
            claim.receipt_id,
            state="applied",
            completed_at_unix_nano=time.time_ns(),
            lease_token=claim.lease_token,
            bundle_id=linked["bundle_id"],
            canonical_sha256=linked["digest"],
        )
        value_hmac = store.fingerprint(IDENTITY_NAMESPACE, IDENTITY_VALUE)
        store.record_external_identity(
            connector,
            key_kind=IDENTITY_NAMESPACE,
            value_hmac=value_hmac,
            bundle_id=linked["bundle_id"],
        )
        reference = None
        if hasattr(store, "set_external_reference"):  # 553cecf onwards
            reference = {
                "bundle_id": linked["bundle_id"],
                "namespace": "upgrade-fixture",
                "record_type": "ticket",
                "external_id": "ticket-1",
            }
            store.set_external_reference(
                linked["bundle_id"],
                project_id=PROJECT_ID,
                namespace=reference["namespace"],
                record_type=reference["record_type"],
                external_id=reference["external_id"],
            )

        purged = next(incident for incident in incidents if incident["source"] == PURGED_SOURCE)
        store.purge(purged["bundle_id"], project_id=purged["project_id"])
        incidents.remove(purged)
    finally:
        store.close()
    return {
        "analyzer_version": ANALYZER_VERSION,
        "project": {"project_id": project.project_id, "display_name": project.display_name},
        "api_keys": {
            "active": {"key_id": active.key_id, "credential": active.credential},
            "revoked": {"key_id": revoked.key_id, "credential": revoked.credential},
        },
        "connector": {"endpoint_id": connector.endpoint_id, "provider": connector.provider},
        "delivery": {"receipt_id": claim.receipt_id, "bundle_id": linked["bundle_id"]},
        "external_identity": {
            "namespace": IDENTITY_NAMESPACE,
            "value": IDENTITY_VALUE,
            "value_hmac": value_hmac,
            "bundle_id": linked["bundle_id"],
        },
        "external_reference": reference,
        "incidents": incidents,
        "purged": purged,
    }


def _drive_open(data_dir: Path, source: Path, schema_version: int) -> dict[str, Any]:
    storage = _release_storage(source, schema_version)
    try:
        storage.IncidentStore(data_dir).close()
    except storage.StorageError as error:
        return {"schema_version": schema_version, "refused": str(error)}
    return {"schema_version": schema_version, "opened": True}


def _driver(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="make_upgrade_fixtures.py --driver")
    parser.add_argument("mode", choices=("generate", "open"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--schema-version", type=int, required=True)
    options = parser.parse_args(arguments)
    drive = _drive_generate if options.mode == "generate" else _drive_open
    print(json.dumps(drive(options.data_dir, options.source, options.schema_version)))
    return 0


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["--driver"]:
        return _driver(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true", help="replace existing fixtures")
    parser.add_argument("--open-with", metavar="RELEASE", choices=[r.name for r in RELEASES])
    parser.add_argument("--data-dir", type=Path)
    options = parser.parse_args(arguments)
    if options.open_with:
        if options.data_dir is None:
            parser.error("--open-with needs --data-dir")
        return open_with(options.open_with, options.data_dir)
    generate(options.output, force=options.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
