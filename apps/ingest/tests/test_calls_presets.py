"""FR-3: every project has a capture preset; 0.1.x upgrades keep their privacy posture.

``projects.capture_preset`` (revision ``0004_calls``) is ``full`` or
``metadata_only``. Every catalog 0.2.0 creates, on SQLite or Postgres, starts
``full``, and so does every project created later. A catalog opened through the
legacy bootstrap (a real 0.1.x file, ``fixtures/upgrade``) gets ``metadata_only``
for every project it already had, in the transaction that stamps the baseline,
with one log line that gives the count. ``redacted`` is not available in this
release (``EARSHOT_PRESET_UNAVAILABLE``).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.storage import (
    CapturePresetUnavailableError,
    IncidentStore,
    ProjectInactiveError,
)
from store_support import (
    TEST_CORRELATION_KEY_BASE64,
    catalog_rows,
    current_dialect,
    make_store,
    postgres_cluster,
)
from upgrade_fixtures import copy_upgrade_fixture, upgrade_fixture_names

pytestmark = pytest.mark.integration

V19 = upgrade_fixture_names()[-1]
LEGACY_PRESET_LOG = "EARSHOT_LEGACY_CAPTURE_PRESET"


def _presets(path: Path) -> dict[str, str]:
    return dict(catalog_rows(path, "SELECT project_id, capture_preset FROM projects"))


# --- Defaults ---------------------------------------------------------------------


def test_a_new_catalog_and_every_later_project_capture_in_full(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")

    assert _presets(tmp_path) == {"default": "full", "support": "full"}
    project = store.get_project("support")
    assert (project.capture_preset, project.lifecycle_state) == ("full", "active")
    assert store.get_project("no-such-project") is None


@pytest.mark.sqlite_only
@pytest.mark.parametrize("incidents", ["with_incidents", "without_incidents"])
def test_every_project_of_an_upgraded_legacy_catalog_becomes_metadata_only(
    tmp_path, caplog, incidents
) -> None:
    data_dir = copy_upgrade_fixture(V19, tmp_path / V19)
    if incidents == "without_incidents":
        _delete_every_incident(data_dir)

    with caplog.at_level(logging.INFO, logger="earshot.storage_migrations"):
        store = IncidentStore(data_dir)

    assert _presets(data_dir) == {"default": "metadata_only", "upgrade-fixture": "metadata_only"}
    assert [
        record.getMessage() for record in caplog.records if LEGACY_PRESET_LOG in record.getMessage()
    ] == [
        f"{LEGACY_PRESET_LOG}: 2 existing project(s) of an upgraded 0.1.x catalog now "
        "capture metadata_only; run `earshot project set-capture` to change it"
    ]

    store.create_project("after-upgrade", display_name="After upgrade")
    assert store.get_project("after-upgrade").capture_preset == "full"
    assert store.get_project("default").capture_preset == "metadata_only"
    store.close()

    # Reopening never re-applies the legacy downgrade.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="earshot.storage_migrations"):
        IncidentStore(data_dir).close()
    assert _presets(data_dir)["after-upgrade"] == "full"
    assert not [record for record in caplog.records if LEGACY_PRESET_LOG in record.getMessage()]


def _delete_every_incident(data_dir: Path) -> None:
    """Make the real v19 fixture a v19 catalog without incidents (its children cascade)."""

    with closing(sqlite3.connect(data_dir / "earshot.sqlite3")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("DELETE FROM incidents")
            connection.execute("DELETE FROM pending_object_deletions")
        assert connection.execute("SELECT count(*) FROM incidents").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (19,)
    shutil.rmtree(data_dir / "objects")


# --- Setting a preset ---------------------------------------------------------------


def test_a_project_preset_can_be_switched_between_the_available_presets(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")

    assert store.set_capture_preset("support", "metadata_only").capture_preset == "metadata_only"
    assert _presets(tmp_path) == {"default": "full", "support": "metadata_only"}
    assert store.set_capture_preset("support", "full").capture_preset == "full"
    assert _presets(tmp_path) == {"default": "full", "support": "full"}


def test_redacted_is_refused_as_unavailable_and_nothing_changes(tmp_path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(CapturePresetUnavailableError) as refused:
        store.set_capture_preset("default", "redacted")

    assert refused.value.code == "EARSHOT_PRESET_UNAVAILABLE"
    assert refused.value.status_code == 422
    assert str(refused.value).startswith("EARSHOT_PRESET_UNAVAILABLE: ")
    with pytest.raises(ValueError, match="full or metadata_only"):
        store.set_capture_preset("default", "everything")
    with pytest.raises(ValueError, match="does not exist"):
        store.set_capture_preset("no-such-project", "full")
    assert _presets(tmp_path) == {"default": "full"}


def test_a_deleting_project_keeps_its_preset(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("leaving", display_name="Leaving")
    store.begin_project_deletion("leaving")

    with pytest.raises(ProjectInactiveError):
        store.set_capture_preset("leaving", "metadata_only")
    assert _presets(tmp_path)["leaving"] == "full"


# --- CLI ----------------------------------------------------------------------------


@pytest.fixture
def cli_store(tmp_path, monkeypatch) -> list[str]:
    """The store arguments for a CLI call against this test's dialect."""

    make_store(tmp_path).close()
    if current_dialect() == "sqlite":
        return ["--data-dir", str(tmp_path)]
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
    return ["--database-url", postgres_cluster().database_for(tmp_path)]


def test_the_cli_sets_and_shows_a_project_capture_preset(tmp_path, cli_store, capsys) -> None:
    assert main(["project", "create", "support", "--display-name", "Support", *cli_store]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "project",
                "set-capture",
                "--project",
                "support",
                "--preset",
                "metadata_only",
                *cli_store,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "project_id": "support",
        "capture_preset": "metadata_only",
    }
    assert main(["project", "show", "support", *cli_store]) == 0
    shown = json.loads(capsys.readouterr().out)

    assert {key: shown[key] for key in ("project_id", "capture_preset", "lifecycle_state")} == {
        "project_id": "support",
        "capture_preset": "metadata_only",
        "lifecycle_state": "active",
    }
    assert shown["display_name"] == "Support"
    assert int(shown["created_at_unix_nano"]) > 0


def test_the_cli_refuses_redacted_with_exit_2_and_the_stable_code(cli_store, capsys) -> None:
    arguments = ["project", "set-capture", "--project", "default", "--preset", "redacted"]

    assert main([*arguments, *cli_store]) == 2

    error = capsys.readouterr().err
    assert error.startswith("earshot: EARSHOT_PRESET_UNAVAILABLE: ")
    assert main(["project", "show", "default", *cli_store]) == 0
    assert json.loads(capsys.readouterr().out)["capture_preset"] == "full"


def test_the_cli_reports_an_unknown_project_and_refuses_an_unknown_preset(
    cli_store, capsys
) -> None:
    assert main(["project", "show", "no-such-project", *cli_store]) == 2
    assert capsys.readouterr().err.strip() == "earshot: project no-such-project does not exist"

    with pytest.raises(SystemExit) as usage:
        main(["project", "set-capture", "--project", "default", "--preset", "everything"])
    assert usage.value.code == 2
    assert "invalid choice: 'everything'" in capsys.readouterr().err
