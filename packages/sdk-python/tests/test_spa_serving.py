from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from earshot.api import create_app

pytestmark = pytest.mark.integration


def _write_spa(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>Earshot</title><div id=root></div>")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('earshot')")


def _client(tmp_path: Path, *, with_spa: bool) -> TestClient:
    web = tmp_path / "web"
    if with_spa:
        _write_spa(web)
    # For the headless case, point at a directory with no index.html so the
    # packaged default (if the SPA was bundled into the package) is bypassed.
    app = create_app(
        data_dir=tmp_path / "data",
        web_dir=web if with_spa else tmp_path / "no-web",
    )
    # A loopback Host header keeps the request on the trusted path.
    return TestClient(app, base_url="http://127.0.0.1")


def test_serves_index_at_root(tmp_path) -> None:
    response = _client(tmp_path, with_spa=True).get("/")
    assert response.status_code == 200
    assert "Earshot" in response.text
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"


def test_client_routes_fall_back_to_index(tmp_path) -> None:
    # A deep client-side route must resolve to the SPA shell, not a 404.
    response = _client(tmp_path, with_spa=True).get("/sessions/bundle-abc")
    assert response.status_code == 200
    assert "id=root" in response.text


def test_static_assets_are_served(tmp_path) -> None:
    response = _client(tmp_path, with_spa=True).get("/assets/app.js")
    assert response.status_code == 200
    assert "earshot" in response.text


def test_unknown_api_paths_stay_json_not_html(tmp_path) -> None:
    # The SPA fallback must never shadow the API: unknown /v1 paths stay JSON.
    response = _client(tmp_path, with_spa=True).get("/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EARSHOT_NOT_FOUND"


def test_health_still_works_behind_the_spa(tmp_path) -> None:
    assert _client(tmp_path, with_spa=True).get("/healthz").status_code == 200


def test_headless_without_a_web_dir(tmp_path) -> None:
    client = _client(tmp_path, with_spa=False)
    assert client.get("/healthz").status_code == 200
    # No catch-all is registered, so the root is a plain 404.
    assert client.get("/").status_code == 404
