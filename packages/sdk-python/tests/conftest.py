from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from earshot.contract import IncidentBundle  # noqa: E402
from incident_factory import make_valid_bundle  # noqa: E402


@pytest.fixture
def valid_bundle() -> IncidentBundle:
    return make_valid_bundle()


@pytest.fixture
def bundle_factory() -> Callable[..., IncidentBundle]:
    return make_valid_bundle
