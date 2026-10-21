"""Fail a release when its wheel omits required runtime files.

This deliberately inspects the built artifact rather than the source tree: users
install the wheel, and optional Hatch artifact globs do not fail when the viewer was
never built.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_point_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_point_names) != 1:
            raise SystemExit(f"{path}: wheel metadata or CLI entry points are missing")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        entry_points = archive.read(entry_point_names[0]).decode("utf-8")
    required_runtime_files = {
        "earshot/__init__.py",
        "earshot/call_materializer.py",
        "earshot/cli.py",
        "earshot/generated/earshot/v1alpha1/incident_pb2.py",
        "earshot/otlp_receiver.py",
        "earshot/recordings.py",
        "earshot/storage_call_purge.py",
        "earshot/storage_call_reads.py",
        "earshot/storage_closer.py",
        "earshot/storage_recordings.py",
        "earshot/storage_analysis.py",
        "earshot/leased_jobs.py",
        "earshot/signals/__init__.py",
        "earshot/signals/child.py",
        "earshot/signals/runner.py",
        # The vendored Silero VAD model the analysis runs, and its licence.
        "earshot/signals/model/LICENSE",
        "earshot/signals/model/silero_vad.onnx",
        "earshot/storage_migrations/__init__.py",
        "earshot/storage_migrations/versions/0001_v19_baseline.py",
        "earshot/storage_migrations/versions/0002_artifacts.py",
        "earshot/storage_migrations/versions/0003_projection_version.py",
        "earshot/storage_migrations/versions/0004_calls.py",
        "earshot/web/index.html",
    }
    missing = required_runtime_files - names
    if missing:
        raise SystemExit(f"{path}: required runtime files are missing: {sorted(missing)!r}")
    # Alembic is configured in code; an ini file or a template must not ship.
    migration_config = sorted(
        name for name in names if name.startswith("earshot/") and name.endswith((".ini", ".mako"))
    )
    if migration_config:
        raise SystemExit(
            f"{path}: migration config or templates must not ship: {migration_config!r}"
        )
    if not any(name.startswith("earshot/web/assets/") for name in names):
        raise SystemExit(f"{path}: bundled viewer assets are missing")
    if "earshot = earshot.cli:main" not in entry_points:
        raise SystemExit(f"{path}: public earshot CLI entry point is missing")

    server_requirements = {
        dependency: [
            line
            for line in metadata.splitlines()
            if line.lower().startswith(f"requires-dist: {dependency}")
        ]
        for dependency in ("alembic", "fastapi", "sqlalchemy", "uvicorn")
    }
    for dependency, requirements in server_requirements.items():
        server_guard = re.compile(r"extra\s*==\s*['\"]server['\"]")
        extra_guard = re.compile(r"extra\s*==\s*['\"][a-z0-9_-]+['\"]")
        if not any(server_guard.search(requirement) for requirement in requirements) or any(
            extra_guard.search(requirement) is None for requirement in requirements
        ):
            raise SystemExit(
                f"{path}: {dependency} must be optional and provided by the server extra"
            )
    # The analysis libraries are the signals extra's: the base and server installs
    # run without them.
    for dependency in ("numpy", "onnxruntime", "soundfile"):
        requirements = [
            line
            for line in metadata.splitlines()
            if line.lower().startswith(f"requires-dist: {dependency}")
        ]
        signals_guard = re.compile(r"extra\s*==\s*['\"]signals['\"]")
        if not any(signals_guard.search(requirement) for requirement in requirements) or any(
            re.search(r"extra\s*==", requirement) is None for requirement in requirements
        ):
            raise SystemExit(
                f"{path}: {dependency} must be optional and provided by the signals extra"
            )


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise SystemExit("usage: check_wheel.py PATH_TO_WHEEL")
    path = Path(arguments[0])
    if not path.is_file() or path.suffix != ".whl":
        raise SystemExit(f"{path}: wheel does not exist")
    check_wheel(path)
    print(f"wheel contains bundled viewer: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
