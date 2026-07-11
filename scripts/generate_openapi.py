"""Generate or verify the public Earshot backend OpenAPI contract."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "spec" / "backend-api.openapi.json"
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from earshot.analysis import ANALYZER_VERSION, analyze_incident  # noqa: E402
from earshot.api import ApiConfig, create_app  # noqa: E402
from earshot.storage import IncidentStore  # noqa: E402


def rendered_schema() -> bytes:
    with tempfile.TemporaryDirectory(prefix="earshot-openapi-") as directory:
        store = IncidentStore(directory)
        try:
            app = create_app(
                store=store,
                analyzer=analyze_incident,
                config=ApiConfig(analyzer_version=ANALYZER_VERSION),
            )
            schema = app.openapi()
        finally:
            store.close()
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = rendered_schema()
    if arguments.check:
        if not TARGET.exists() or TARGET.read_bytes() != expected:
            raise SystemExit("generated OpenAPI drift: spec/backend-api.openapi.json")
        print("generated OpenAPI artifact is current")
        return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(expected)
    print("generated spec/backend-api.openapi.json")


if __name__ == "__main__":
    main()
