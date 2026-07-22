from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_container_build_context_excludes_local_secrets() -> None:
    patterns = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".env", ".env.*", "local", "local/**", "*.key", "*.pem"} <= patterns


def test_viewer_stage_copies_only_required_workspace_inputs() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()

    assert "COPY . ." not in dockerfile
