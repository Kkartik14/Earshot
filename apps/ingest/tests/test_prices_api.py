"""Prices over HTTP: ``GET /v1/prices`` and ``POST /v1/prices``.

Every test runs the real app on a real uvicorn server over a real catalog (both
dialects). Built-in prices (``project_id`` NULL) are written by the operator, not
through the API: the server seeds the defaults at start, in the background, and a
test writes its own with ``PriceStore`` as the operator CLI does. Assertions on rows
and versions are therefore per scope: the projects' own, or every built-in.
"""

from __future__ import annotations

import json
import time

import pytest

from earshot.api import create_app
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.storage_prices import NewPrice, PriceStore
from otlp_support import request, serve
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

PRICE = {
    "provider": "Groq",
    "model": "OpenAI/GPT-OSS-20B",
    "unit": "input_token",
    "price_nano_usd": 75,
    "per_quantity": 1,
    "effective_from_unix_nano": "1790000000000000000",
    "source_url": "https://groq.com/pricing",
    "note": "regular list price, as of 2026-10-19",
}


def _post(port: int, body: dict, *, credential: str | None = None):
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return request(port, "POST", "/v1/prices", json.dumps(body).encode(), headers=headers)


def _versions(tmp_path) -> dict[str, int]:
    """Each project's own price version (the built-in one moves with the seeding)."""

    return dict(catalog_rows(tmp_path, "SELECT project_id, price_version FROM projects"))


def _wait_for_seeded_built_ins(tmp_path) -> None:
    """Until the server's start-up seeding of the default prices has committed."""

    deadline = time.monotonic() + 30
    while catalog_rows(tmp_path, "SELECT count(*) FROM prices WHERE project_id IS NULL") < [
        (len(BUILTIN_PRICES),)
    ]:
        assert time.monotonic() < deadline, "the built-in prices were not seeded"
        time.sleep(0.05)


def _project_rows(tmp_path) -> list[tuple]:
    return catalog_rows(
        tmp_path,
        "SELECT project_id, price_nano_usd FROM prices WHERE project_id IS NOT NULL ORDER BY id",
    )


def test_a_posted_price_is_the_callers_own_lower_cased_and_bumps_only_its_price_version(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.create_project("other", display_name="Other")

    with serve(create_app(store=store)) as port:
        created = _post(port, PRICE)
        again = _post(port, {**PRICE, "price_nano_usd": 80, "per_quantity": 1000})

    assert created.status == 201, created.body[:300]
    body = created.json()
    assert body == {
        "id": body["id"],
        "scope": "project",
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
        "unit": "input_token",
        "price_nano_usd": 75,
        "per_quantity": 1,
        "effective_from_unix_nano": "1790000000000000000",
        "source_url": "https://groq.com/pricing",
        "note": "regular list price, as of 2026-10-19",
        "created_unix_nano": body["created_unix_nano"],
    }
    assert isinstance(body["id"], int) and body["created_unix_nano"].isdigit()
    assert again.status == 201 and again.json()["id"] > body["id"]
    # Append-only: both rows stay; each insert bumps the project's own version only.
    assert _project_rows(tmp_path) == [
        ("default", 75),
        ("default", 80),
    ]
    assert _versions(tmp_path) == {"default": 2, "other": 0}


def test_get_prices_lists_the_built_ins_and_the_callers_rows_in_pages(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("other", display_name="Other")
    other = store.issue_api_key("other", label="other").credential
    builtin = PriceStore(store).add(
        None,
        NewPrice(
            provider="deepgram",
            model="nova-3-general",
            unit="audio_second",
            price_nano_usd=7_700_000,
            per_quantity=60,
            effective_from_unix_nano=0,
            source_url="https://deepgram.com/pricing",
            note=None,
        ),
    )

    with serve(create_app(store=store)) as port:
        own = _post(port, PRICE).json()
        foreign = _post(port, {**PRICE, "provider": "cartesia"}, credential=other).json()
        _wait_for_seeded_built_ins(tmp_path)
        pages = []
        cursor = None
        while True:
            suffix = "" if cursor is None else f"&cursor={cursor}"
            pages.append(request(port, "GET", f"/v1/prices?limit=7{suffix}").json())
            cursor = pages[-1]["next_cursor"]
            if cursor is None:
                break
        theirs = request(
            port, "GET", "/v1/prices?limit=100", headers={"Authorization": f"Bearer {other}"}
        ).json()
        bad_cursor = request(port, "GET", "/v1/prices?cursor=nope")

    listed = [item for page in pages for item in page["items"]]
    builtins = [
        row
        for (row,) in catalog_rows(
            tmp_path, "SELECT id FROM prices WHERE project_id IS NULL ORDER BY id"
        )
    ]
    assert builtin.project_id is None and builtin.id in builtins
    # Every built-in and the caller's own row, oldest first, each once, in pages of 7.
    assert [item["id"] for item in listed] == sorted([*builtins, own["id"]])
    assert {item["id"]: item["scope"] for item in listed}[own["id"]] == "project"
    assert all(len(page["items"]) <= 7 for page in pages) and len(pages) > 1
    assert [item["id"] for item in theirs["items"] if item["scope"] == "project"] == [foreign["id"]]
    assert builtin.id in [item["id"] for item in theirs["items"]]
    assert (bad_cursor.status, bad_cursor.problem_code()) == (400, "EARSHOT_INVALID_CURSOR")
    assert _versions(tmp_path) == {"default": 1, "other": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "token"},
        {"unit": None},
        {"provider": ""},
        {"provider": "a b"},
        {"provider": "x" * 129},
        {"model": "éclair"},
        {"model": "m" * 257},
        {"price_nano_usd": -1},
        {"price_nano_usd": 2**63},
        {"price_nano_usd": 1.5},
        {"per_quantity": 0},
        {"effective_from_unix_nano": "-1"},
        {"effective_from_unix_nano": "9223372036854775808"},
        {"effective_from_unix_nano": 0},
        {"source_url": "ftp://example.com/prices"},
        {"note": "a\x00b"},
        {"currency": "EUR"},
    ],
)
def test_an_invalid_price_is_422_and_writes_nothing(tmp_path, change) -> None:
    store = make_store(tmp_path)
    body = {key: value for key, value in {**PRICE, **change}.items() if value is not None}
    if change.get("unit", "") is None:
        body.pop("unit", None)

    with serve(create_app(store=store)) as port:
        reply = _post(port, body)

    assert (reply.status, reply.problem_code()) == (422, "EARSHOT_INVALID_REQUEST")
    assert _project_rows(tmp_path) == []
    assert _versions(tmp_path) == {"default": 0}
