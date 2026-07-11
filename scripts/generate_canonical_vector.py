"""Generate the language-neutral full-envelope RFC 8785 conformance vector."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from earshot import codec  # noqa: E402

SOURCE = ROOT / "fixtures" / "faults" / "slow_endpointing.incident.json"
INPUT = ROOT / "fixtures" / "conformance" / "canonical-vector.input.json"
EXPECTED = ROOT / "fixtures" / "conformance" / "canonical-vector.expected.json"


def main() -> None:
    value = json.loads(SOURCE.read_text(encoding="utf-8"))
    value["profile"]["manifest"]["bundle_id"] = "canonical-vector-v1"
    value["profile"]["session"]["ended_at"] = None
    value["profile"]["participants"][0]["endpoint_kind"] = None
    value["profile"]["attributes"] = {
        "earshot.metric.vector.large": 1e20,
        "earshot.metric.vector.negative_zero": -0.0,
        "earshot.metric.vector.tiny": 1e-7,
        "service.name": "Ångström 😀",
    }
    bundle = codec.decode_incident_json(json.dumps(value, ensure_ascii=False))
    envelope_bytes = codec.encode_incident_protobuf(bundle)
    envelope = codec._IncidentEnvelopeMessage()
    envelope.ParseFromString(envelope_bytes)
    canonical_profile = bytes(envelope.canonical_profile_json)

    INPUT.parent.mkdir(parents=True, exist_ok=True)
    INPUT.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    EXPECTED.write_text(
        json.dumps(
            {
                "canonical_profile_json": canonical_profile.decode("utf-8"),
                "profile_sha256": hashlib.sha256(canonical_profile).hexdigest(),
                "envelope_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"generated {INPUT.relative_to(ROOT)} and {EXPECTED.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
