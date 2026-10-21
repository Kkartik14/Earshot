"""The operator CLI for prices and the transcript index (AC4.4, M5, M9).

``earshot prices add|list`` and ``earshot db enable-transcript-index`` run through
``earshot.cli.main`` against the real catalog of the dialect under test. Only the
operator writes built-in prices (``--builtin``); a project's own rows are scoped
to it, as ``POST /v1/prices`` writes them.
"""

from __future__ import annotations

import json

import pytest

from catalog_snapshot import postgres_catalog_snapshot
from earshot import cli
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage_call_reads import CallFilters, CallReads
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingStore
from otlp_support import make_request, make_span, string_attribute
from projection_support import insert_projection
from store_support import (
    TEST_CORRELATION_KEY_BASE64,
    catalog_rows,
    current_dialect,
    make_store,
    postgres_cluster,
)

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


def _price(**changes: str) -> list[str]:
    values = {
        "--provider": "Deepgram",
        "--model": "nova-3",
        "--unit": "audio_second",
        "--price-nano-usd": "7700000",
        "--per-quantity": "60",
        "--effective-from-unix-nano": "0",
        "--source-url": "https://deepgram.com/pricing",
        "--note": "regular list price",
        **{f"--{key.replace('_', '-')}": value for key, value in changes.items()},
    }
    return [part for pair in values.items() for part in pair]


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    location = ["--data-dir", str(tmp_path)]
    if current_dialect() == "postgresql":
        monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
        location += ["--database-url", postgres_cluster().database_for(tmp_path)]

    def run(*arguments: str) -> tuple[int, str, str]:
        code = cli.main([*arguments[:2], *location, *arguments[2:]])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def test_the_operator_adds_built_in_and_project_prices_and_lists_them(tmp_path, run) -> None:
    make_store(tmp_path).close()
    builtin = run("prices", "add", "--builtin", *_price())
    own = run("prices", "add", "--project", "default", *_price(price_nano_usd="8000000"))
    listed = run("prices", "list", "--project", "default")

    assert builtin[0] == 0 and own[0] == 0, (builtin, own)
    added = json.loads(builtin[1])
    assert {key: added[key] for key in ("scope", "provider", "model", "price_nano_usd")} == {
        "scope": "builtin",
        "provider": "deepgram",
        "model": "nova-3",
        "price_nano_usd": 7_700_000,
    }
    assert [(item["scope"], item["price_nano_usd"]) for item in json.loads(listed[1])] == [
        ("builtin", 7_700_000),
        ("project", 8_000_000),
    ]
    assert catalog_rows(tmp_path, "SELECT version FROM builtin_price_version") == [(1,)]
    assert catalog_rows(
        tmp_path, "SELECT price_version FROM projects WHERE project_id = 'default'"
    ) == [(1,)]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--builtin", *_price(unit="token")],
        ["--project", "default", *_price(price_nano_usd="-1")],
        ["--project", "default", *_price(per_quantity="0")],
        ["--project", "nope", *_price()],
    ],
    ids=["unit", "price", "per-quantity", "project"],
)
def test_an_invalid_price_is_refused_and_writes_nothing(tmp_path, run, arguments) -> None:
    make_store(tmp_path).close()
    code, _, err = run("prices", "add", *arguments)

    assert code == 2, err
    assert err.startswith("earshot:")
    assert catalog_rows(tmp_path, "SELECT count(*) FROM prices") == [(0,)]


def test_a_price_names_its_scope(tmp_path, run) -> None:
    make_store(tmp_path).close()
    with pytest.raises(SystemExit):
        run("prices", "add", *_price())
    with pytest.raises(SystemExit):
        run("prices", "add", "--builtin", "--project", "default", *_price())


@pytest.mark.postgres_only
def test_the_transcript_index_is_one_optional_index_outside_the_schema(tmp_path, run) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    attributes = [string_attribute("earshot.session.id", "indexed")]
    calls.record_spans(
        "default", spans_from_request(make_request([make_span(1, 1, attributes=attributes)]))
    )
    call_id = calls.call_id("default", "indexed", 0)
    clock.advance(CallTimings().long_quiet_nano)
    Closer(
        store, calls=calls, recordings=RecordingStore(store, calls=calls, blobs=None)
    ).close_next()
    insert_projection(tmp_path, call_id, transcript=[("user", "A quiet table by the WINDOW")])
    reads = CallReads(store)
    url = postgres_cluster().database_for(tmp_path)
    before = postgres_catalog_snapshot(url)
    searched = [
        item.call_id for item in reads.list_calls("default", filters=CallFilters(q="window")).items
    ]

    first = run("db", "enable-transcript-index")
    again = run("db", "enable-transcript-index")

    assert (first[0], again[0]) == (0, 0), (first, again)
    assert json.loads(first[1]) == {"index": "earshot_opt_call_transcripts_text_trgm"}
    assert catalog_rows(
        tmp_path,
        "SELECT indexname, indexdef LIKE '%gin%gin_trgm_ops%' FROM pg_indexes "
        "WHERE indexname LIKE 'earshot\\_opt\\_%'",
    ) == [("earshot_opt_call_transcripts_text_trgm", True)]
    # Catalog parity ignores it: the schema is still the migration chain's.
    assert postgres_catalog_snapshot(url) == before
    # Search answers the same through it.
    assert searched == [call_id]
    assert [
        item.call_id for item in reads.list_calls("default", filters=CallFilters(q="window")).items
    ] == [call_id]


@pytest.mark.sqlite_only
def test_on_sqlite_the_transcript_index_is_refused_with_its_reason(tmp_path, run) -> None:
    make_store(tmp_path).close()
    code, _, err = run("db", "enable-transcript-index")

    assert code == 2
    assert "Postgres" in err
