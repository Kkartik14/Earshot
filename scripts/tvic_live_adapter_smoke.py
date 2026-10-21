#!/usr/bin/env python3
"""Exercise Earshot's TVIC adapter with one live TVIC provider-stack run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/sdk-python/src"))

from earshot.adapters.tvic import TVICRuntimeAdapter, TVICRuntimeObservationSink  # noqa: E402
from earshot.analysis import analyze_incident  # noqa: E402
from earshot.codec import analysis_input_sha256  # noqa: E402
from earshot.validation import validate_incident  # noqa: E402

_EXPECTED_LIFECYCLE = ("session.start", "turn.start", "turn.end", "session.end")
_PRIVATE_KEYS = frozenset(
    {"audio", "model_output", "prompt", "provider_payload", "transcript", "transcription"}
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tvic-dir", type=Path, required=True)
    args = parser.parse_args()
    tvic_dir = args.tvic_dir.expanduser().resolve()
    if not (tvic_dir / "scripts/provider-stack-smoke.ts").is_file():
        parser.error("--tvic-dir must point to a TVIC checkout with provider-stack-smoke.ts")

    with tempfile.TemporaryDirectory(prefix="earshot-tvic-live-") as temporary_directory:
        capture_path = Path(temporary_directory) / "observations.json"
        env = os.environ.copy()
        env["TVIC_EARSHOT_OBSERVATION_PATH"] = str(capture_path)
        result = subprocess.run(["pnpm", "provider:smoke"], cwd=tvic_dir, env=env, check=False)
        if result.returncode != 0:
            print(f"TVIC provider smoke failed with exit code {result.returncode}", file=sys.stderr)
            return result.returncode or 1
        evidence_path = tvic_dir / "local/task-2/provider-stack-smoke.json"
        if not evidence_path.is_file():
            print("TVIC provider smoke did not persist its evidence report", file=sys.stderr)
            return 1
        evidence = json.loads(evidence_path.read_text("utf-8"))
        final_session = evidence["results"]["transport"]["finalSession"]
        if final_session.get("sessionId") != "redacted" or (
            "callId" in final_session and final_session["callId"] != "redacted"
        ):
            print("TVIC provider smoke evidence retains a real session identifier", file=sys.stderr)
            return 1
        if not capture_path.is_file():
            print(
                "TVIC smoke completed without producing the requested observation capture",
                file=sys.stderr,
            )
            return 1

        observations = cast(list[dict[str, object]], json.loads(capture_path.read_text("utf-8")))
        names = tuple(str(observation.get("name")) for observation in observations)
        if not all(name in names for name in _EXPECTED_LIFECYCLE):
            print(f"TVIC producer omitted lifecycle facts: {names}", file=sys.stderr)
            return 1
        if names.index("turn.end") > names.index("session.end"):
            print("TVIC session.end arrived before turn.end", file=sys.stderr)
            return 1
        if any(_contains_private_key(observation) for observation in observations):
            print("TVIC observation capture contains a prohibited payload field", file=sys.stderr)
            return 1
        if any(
            observation.get("sessionId") != "live-smoke-session" for observation in observations
        ):
            print(
                "TVIC smoke capture contains an unpseudonymized session identifier", file=sys.stderr
            )
            return 1
        if any(
            not str(observation.get("epoch", "")).startswith("live-smoke-epoch-")
            or not str(observation.get("factId", "")).startswith("live-smoke-fact-")
            or (
                observation.get("turnId") is not None
                and not str(observation["turnId"]).startswith("live-smoke-turn-")
            )
            or (
                isinstance(observation.get("attributes"), dict)
                and observation["attributes"].get("agent_id") != "live-smoke-agent"
                and "agent_id" in observation["attributes"]
            )
            for observation in observations
        ):
            print("TVIC smoke capture contains an unpseudonymized identifier", file=sys.stderr)
            return 1

        adapter = TVICRuntimeAdapter.create(
            session_id="live-smoke-session",
            started_at_unix_nano=time.time_ns(),
        )
        sink = TVICRuntimeObservationSink(adapter)
        for observation in observations:
            sink.enqueue(observation)
        bundle = sink.close()
        if sink.dropped_count != 0 or sink.error is not None:
            print("Earshot rejected or dropped a live TVIC observation", file=sys.stderr)
            return 1
        if adapter.accepted_observations != len(observations):
            print("Earshot did not admit every live TVIC observation", file=sys.stderr)
            return 1
        if bundle.profile.manifest.finality != "final":
            print("Earshot did not observe the live TVIC session end", file=sys.stderr)
            return 1
        if bundle.profile.manifest.completeness != "complete":
            print("Earshot marked the live TVIC incident incomplete", file=sys.stderr)
            return 1

        report = validate_incident(bundle)
        if not report.ok:
            print(
                "Earshot incident validation failed: "
                + ", ".join(issue.code for issue in report.errors),
                file=sys.stderr,
            )
            return 1
        analysis = analyze_incident(
            bundle,
            input_sha256=analysis_input_sha256(bundle),
            generated_at_unix_nano=time.time_ns(),
        )
        if not analysis.projections.turns:
            print("Earshot analysis produced no turn projection for the live call", file=sys.stderr)
            return 1

        print(
            "Live TVIC→Earshot proof passed: "
            f"facts={len(observations)}, lifecycle={','.join(_EXPECTED_LIFECYCLE)}, "
            "finality=final, completeness=complete, analyzed_turns="
            f"{len(analysis.projections.turns)}"
        )
    return 0


def _contains_private_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _PRIVATE_KEYS or _contains_private_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_private_key(item) for item in value)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
