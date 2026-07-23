"""Generate viewer fixtures from the current deterministic backend explanation."""

from __future__ import annotations

import argparse
import json
import pathlib

from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, decode_incident_json
from earshot.explanation import explain_incident
from earshot.validation import validate_derived_analysis, validate_explanation

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAULTS = ROOT / "fixtures" / "faults"
OUTPUT = ROOT / "apps" / "viewer" / "src" / "features" / "inspector" / "__fixtures__" / "faults"


def _projection(name: str) -> dict[str, object]:
    bundle = decode_incident_json((FAULTS / f"{name}.incident.json").read_bytes())
    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=0,
    )
    analysis_report = validate_derived_analysis(bundle, analysis)
    if not analysis_report.ok:
        raise SystemExit(f"{name}: generated analysis is invalid")
    explanation = explain_incident(bundle, analysis)
    explanation_report = validate_explanation(bundle, analysis, explanation)
    if not explanation_report.ok:
        raise SystemExit(f"{name}: generated explanation is invalid")
    return explanation.model_dump(mode="json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    names = sorted(
        path.name.removesuffix(".incident.json") for path in FAULTS.glob("*.incident.json")
    )
    stale: list[str] = []
    for name in names:
        destination = OUTPUT / f"{name}.explanation.json"
        projection = _projection(name)
        if arguments.check:
            if not destination.is_file() or json.loads(destination.read_text()) != projection:
                stale.append(str(destination.relative_to(ROOT)))
            continue
        destination.write_text(json.dumps(projection, indent=2) + "\n")
    if stale:
        raise SystemExit(f"viewer explanation fixtures are stale: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
