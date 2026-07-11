from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))
TEST_SUPPORT = ROOT / "packages" / "sdk-python" / "tests"
if str(TEST_SUPPORT) not in sys.path:
    sys.path.insert(0, str(TEST_SUPPORT))

from incident_factory import make_valid_bundle  # noqa: E402


@pytest.fixture
def valid_bundle():
    return make_valid_bundle()
