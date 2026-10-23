"""Repository-wide pytest setup for both test trees.

pytest always loads this file first, whichever test path it is given, so options
that both ``packages/sdk-python/tests`` and ``apps/ingest/tests`` need are
registered here, once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for path in (ROOT / "packages" / "sdk-python" / "src", ROOT / "packages" / "sdk-python" / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

pytest_plugins = ["store_support", "blob_support"]
# Shared assertion helpers, rewritten like test modules so a failure shows its values.
pytest.register_assert_rewrite("call_done_when")
