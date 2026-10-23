"""The storage runtime (SQLAlchemy, Alembic) belongs to the ``server`` extra.

The SDK and the storage-free CLI commands run on a base install; the commands that
open a store say which extra they need instead of failing with an import trace.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("module", ["earshot", "earshot.cli", "earshot.storage"])
def test_importing_sdk_cli_or_storage_does_not_load_the_storage_runtime(module: str) -> None:
    probe = (
        "import importlib, sys\n"
        f"importlib.import_module({module!r})\n"
        "loaded = sorted(name for name in sys.modules\n"
        "                if name.partition('.')[0] in {'sqlalchemy', 'alembic', 'psycopg'})\n"
        "print(','.join(loaded))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert completed.stdout.strip() == ""


def _base_install(workspace: Path) -> Path:
    """Install this checkout with no extras into a fresh virtual environment."""

    environment_dir = workspace / "base-venv"
    python = environment_dir / "bin" / "python"
    uv = shutil.which("uv")
    if uv is not None:
        subprocess.run(
            [uv, "venv", "--quiet", "--python", sys.executable, str(environment_dir)],
            check=True,
            timeout=120,
        )
        subprocess.run(
            [uv, "pip", "install", "--quiet", "--python", str(python), "-e", str(ROOT)],
            check=True,
            timeout=300,
        )
    else:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_dir)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "-e", str(ROOT)],
            check=True,
            timeout=600,
        )
    return environment_dir / "bin"


def test_storage_commands_on_a_base_install_exit_2_naming_the_server_extra(tmp_path) -> None:
    binaries = _base_install(tmp_path)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    data_dir = tmp_path / "data"

    runtime = subprocess.run(
        [str(binaries / "python"), "-c", "import sqlalchemy"],
        capture_output=True,
        env=environment,
        check=False,
    )
    assert runtime.returncode != 0, "the base environment must not provide SQLAlchemy"

    help_output = subprocess.run(
        [str(binaries / "earshot"), "--help"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=60,
    )
    assert help_output.returncode == 0, help_output.stderr

    listed = subprocess.run(
        [str(binaries / "earshot"), "list", "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=60,
    )

    assert listed.returncode == 2
    assert "earshot-observability[server]" in listed.stderr
    assert "Traceback" not in listed.stderr
    assert not data_dir.exists()
