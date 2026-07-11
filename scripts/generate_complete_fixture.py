"""Generate the public valid base used by language-neutral mutation cases."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "tests"))

from earshot.codec import encode_incident_json  # noqa: E402
from incident_factory import make_valid_bundle  # noqa: E402


def main() -> None:
    target = ROOT / "fixtures" / "valid" / "complete.json"
    target.write_bytes(encode_incident_json(make_valid_bundle(), indent=2) + b"\n")
    print(f"generated {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
