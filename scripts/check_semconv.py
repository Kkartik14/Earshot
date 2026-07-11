"""Validate the standalone Earshot semantic-convention authoring registry."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "semconv" / "earshot.yaml"
SOURCE = ROOT / "packages" / "sdk-python" / "src" / "earshot"


def main() -> None:
    document = yaml.safe_load(REGISTRY.read_text())
    groups = document.get("groups", [])
    registry = next(group for group in groups if group.get("id") == "registry.earshot")
    identifiers = [attribute["id"] for attribute in registry.get("attributes", [])]
    if len(identifiers) != len(set(identifiers)):
        raise SystemExit("semantic registry contains duplicate attribute identifiers")

    references = {
        attribute["ref"]
        for group in groups
        for attribute in group.get("attributes", [])
        if "ref" in attribute
    }
    missing_references = sorted(references - set(identifiers))
    if missing_references:
        raise SystemExit("unresolved semantic references: " + ", ".join(missing_references))

    sys.path.insert(0, str(SOURCE))
    from earshot.privacy import _SAFE_EXACT

    required = {key for key in _SAFE_EXACT if key.startswith("earshot.")}
    pattern = re.compile(
        r'["\'](earshot\.(?:metric|duration)\.[a-z0-9_.]+'
        r'|earshot\.analysis\.synthetic_projection)["\']'
    )
    for path in SOURCE.rglob("*.py"):
        required.update(pattern.findall(path.read_text()))
    missing_attributes = sorted(required - set(identifiers))
    if missing_attributes:
        raise SystemExit("unregistered emitted attributes: " + ", ".join(missing_attributes))
    print(f"semantic registry is self-contained ({len(identifiers)} attributes)")


if __name__ == "__main__":
    main()
