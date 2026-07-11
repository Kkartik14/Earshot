"""Generate or verify protobuf bindings and public JSON Schemas.

Run from the repository root after installing the dev extra:
    python scripts/generate_contract.py
    python scripts/generate_contract.py --check
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto" / "earshot" / "v1" / "incident.proto"
GENERATED = ROOT / "packages" / "sdk-python" / "src" / "earshot" / "generated"
GENERATED_BINDING = GENERATED / "earshot" / "v1" / "incident_pb2.py"
INCIDENT_SCHEMA = ROOT / "spec" / "incident-bundle.schema.json"
ANALYSIS_SCHEMA = ROOT / "spec" / "derived-analysis.schema.json"


def _protoc(output: Path) -> Path:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={ROOT / 'proto'}",
            f"--python_out={output}",
            str(PROTO),
        ],
        check=True,
    )
    return output / "earshot" / "v1" / "incident_pb2.py"


def _schema_bytes(model: object, schema_id: str) -> bytes:
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode()


def _schemas() -> dict[Path, bytes]:
    sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))
    from earshot.contract import DerivedAnalysis, IncidentBundleJson

    return {
        INCIDENT_SCHEMA: _schema_bytes(
            IncidentBundleJson,
            "https://schemas.earshot.dev/v1/incident-bundle.schema.json",
        ),
        ANALYSIS_SCHEMA: _schema_bytes(
            DerivedAnalysis,
            "https://schemas.earshot.dev/v1/derived-analysis.schema.json",
        ),
    }


def _check() -> None:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="earshot-contract-") as directory:
        candidate = _protoc(Path(directory)).read_bytes()
    if not GENERATED_BINDING.exists() or GENERATED_BINDING.read_bytes() != candidate:
        failures.append(str(GENERATED_BINDING.relative_to(ROOT)))
    for path, expected in _schemas().items():
        if not path.exists() or path.read_bytes() != expected:
            failures.append(str(path.relative_to(ROOT)))
    if failures:
        raise SystemExit("generated contract drift: " + ", ".join(failures))
    print("generated contract artifacts are current")


def _write() -> None:
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "__init__.py").touch()
    _protoc(GENERATED)
    for directory in [GENERATED / "earshot", GENERATED / "earshot" / "v1"]:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").touch()
    for path, payload in _schemas().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        print(f"generated {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated files drift")
    arguments = parser.parse_args()
    _check() if arguments.check else _write()


if __name__ == "__main__":
    main()
