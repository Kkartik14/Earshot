"""Read access to ``fixtures/upgrade``: catalogs written by historical Earshot code.

``scripts/make_upgrade_fixtures.py`` generated them once, by running each
release's own code extracted from git, and recorded what it wrote in
``fixtures/upgrade/manifest.json``. Tests never regenerate them (CI checkouts
are shallow); they copy a fixture before opening it, so the stored files never
change.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
from pathlib import Path
from typing import Any

UPGRADE_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "upgrade"
FIXTURE_FILES = ("earshot.sqlite3", "instance-correlation.key")


@functools.cache
def upgrade_manifest() -> dict[str, Any]:
    return json.loads((UPGRADE_FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def upgrade_fixture_names() -> list[str]:
    """The fixtures, oldest schema first."""

    fixtures = upgrade_manifest()["fixtures"]
    return sorted(fixtures, key=lambda name: fixtures[name]["schema_version"])


def upgrade_fixture(name: str) -> dict[str, Any]:
    return upgrade_manifest()["fixtures"][name]


def copy_upgrade_fixture(name: str, destination: Path) -> Path:
    """Copy one fixture's data directory to ``destination`` and return it.

    Git keeps no file modes beyond the executable bit, so the copy gets the
    owner-only modes a store's data directory has.
    """

    source = UPGRADE_FIXTURES / upgrade_fixture(name)["directory"]
    destination.mkdir(parents=True, mode=0o700)
    for file_name in FIXTURE_FILES:
        shutil.copyfile(source / file_name, destination / file_name)
        os.chmod(destination / file_name, 0o600)
    shutil.copytree(source / "objects", destination / "objects")
    return destination
