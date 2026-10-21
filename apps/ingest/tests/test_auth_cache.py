"""API-key authentication cost and revocation (FR-2).

A request with an API key costs one indexed, lock-free SELECT of the key's row
(revoked or not, project active or not) every time. The memory-hard scrypt check
runs once per credential per minute: only its positive verdict is cached, keyed
by the credential's SHA-256, never by the credential. ``last_used_at`` is written
at most once a minute per key, in its own short mutation. Revocation comes from
another process (the CLI), so it is tested with a real CLI subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from earshot.api import create_app
from earshot.clock import ManualClock
from earshot.credential_cache import CredentialVerdictCache
from lock_holders import store_write_lock_held_elsewhere
from store_support import (
    TEST_CORRELATION_KEY_BASE64,
    catalog_rows,
    current_dialect,
    make_store,
    postgres_cluster,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


def _client(store) -> TestClient:
    return TestClient(create_app(store=store), base_url="http://127.0.0.1")


def _incidents(client: TestClient, credential: str) -> int:
    return client.get(
        "/v1/incidents", headers={"Authorization": f"Bearer {credential}"}
    ).status_code


def _cli(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment["PYTHONPATH"] = str(ROOT / "packages" / "sdk-python" / "src")
    store_arguments = ["--data-dir", str(path)]
    if current_dialect() == "postgresql":
        environment["EARSHOT_CORRELATION_KEY"] = TEST_CORRELATION_KEY_BASE64
        store_arguments += ["--database-url", postgres_cluster().database_for(path)]
    return subprocess.run(
        [sys.executable, "-m", "earshot.cli", *arguments, *store_arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_repeat_requests_verify_the_secret_once_a_minute(tmp_path) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    credential = store.issue_api_key("default", label="exporter").credential
    client = _client(store)

    assert [_incidents(client, credential) for _ in range(3)] == [200, 200, 200]
    assert store.authentication_stats().secret_verifications == 1

    clock.advance(61 * SECOND)
    assert _incidents(client, credential) == 200
    assert store.authentication_stats().secret_verifications == 2


def test_a_wrong_secret_is_verified_every_time_and_never_cached(tmp_path) -> None:
    store = make_store(tmp_path)
    issued = store.issue_api_key("default", label="exporter")
    wrong = issued.credential[:-4] + ("AAAA" if not issued.credential.endswith("AAAA") else "BBBB")
    client = _client(store)

    assert [_incidents(client, wrong) for _ in range(2)] == [401, 401]
    assert store.authentication_stats().secret_verifications == 2
    assert _incidents(client, issued.credential) == 200
    assert _incidents(client, wrong) == 401
    assert store.authentication_stats().secret_verifications == 4


def test_a_key_revoked_by_the_cli_is_refused_on_its_next_request(tmp_path) -> None:
    store = make_store(tmp_path)
    issued = store.issue_api_key("default", label="exporter")
    client = _client(store)
    assert _incidents(client, issued.credential) == 200  # the verdict is now cached

    revoked = _cli(tmp_path, "api-key", "revoke", "--project", "default", "--key-id", issued.key_id)

    assert revoked.returncode == 0, revoked.stderr
    assert _incidents(client, issued.credential) == 401


def test_last_used_is_written_at_most_once_a_minute_per_key(tmp_path) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    issued = store.issue_api_key("default", label="exporter")

    def last_used() -> int | None:
        [(value,)] = catalog_rows(
            tmp_path,
            "SELECT last_used_at_unix_nano FROM api_keys WHERE key_id = ?",
            (issued.key_id,),
        )
        return value

    assert store.authenticate_api_key(issued.credential) is not None
    assert last_used() == START
    clock.advance(59 * SECOND)
    assert store.authenticate_api_key(issued.credential) is not None
    assert last_used() == START
    clock.advance(2 * SECOND)
    assert store.authenticate_api_key(issued.credential) is not None
    assert last_used() == START + 61 * SECOND


def test_a_busy_store_skips_the_last_used_write_instead_of_queueing_for_it(tmp_path) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    issued = store.issue_api_key("default", label="exporter")
    results: list[object] = []

    with store_write_lock_held_elsewhere(tmp_path):
        worker = threading.Thread(
            target=lambda: results.append(store.authenticate_api_key(issued.credential)),
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        worker.join(timeout=5)
        finished = not worker.is_alive()
        elapsed = time.monotonic() - started
    worker.join(timeout=30)

    # Authenticated at once (the revocation check already ran); the timestamp waits.
    assert finished and elapsed < 1.0
    assert results[0] is not None
    assert catalog_rows(
        tmp_path, "SELECT last_used_at_unix_nano FROM api_keys WHERE key_id = ?", (issued.key_id,)
    ) == [(None,)]
    # The skipped write is retried by the next request, not a minute later.
    assert store.authenticate_api_key(issued.credential) is not None
    assert catalog_rows(
        tmp_path, "SELECT last_used_at_unix_nano FROM api_keys WHERE key_id = ?", (issued.key_id,)
    ) == [(START,)]


def test_the_verdict_cache_is_bounded_and_forgets_verdicts_after_their_lifetime() -> None:
    clock = ManualClock(monotonic=0)
    cache = CredentialVerdictCache(clock=clock, capacity=2, ttl_seconds=60)

    cache.remember("first", "key-1")
    cache.remember("second", "key-2")
    assert cache.verdict("first") == "key-1"  # now the most recently used
    cache.remember("third", "key-3")

    assert cache.verdict("second") is None
    assert (cache.verdict("first"), cache.verdict("third")) == ("key-1", "key-3")
    clock.advance(60 * SECOND)
    assert cache.verdict("first") is None
