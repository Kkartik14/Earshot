"""Ingest many small incidents through the real store, from several processes.

Tests that need a large catalog fill it with real ``IncidentStore.ingest`` calls.
The CPU-bound part of an ingest (validation, encoding, analysis) runs outside the
store's write lock, so worker processes overlap; their writes still serialize on
the store lock exactly as concurrent servers' would.

Run as ``python bulk_ingest.py <data-dir> <first> <count>``; with
``BULK_DATABASE_URL`` set, the store is that Postgres database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def small_incident_id(index: int) -> str:
    return f"incident-{index:05d}"


def ingest_in_parallel(
    data_dir: Path,
    count: int,
    *,
    database_url: str | None = None,
    workers: int | None = None,
) -> None:
    """Ingest ``count`` small incidents into the store at ``data_dir`` (or ``database_url``)."""

    workers = workers or max(1, min(6, (os.cpu_count() or 2) - 1))
    share = -(-count // workers)
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(ROOT / "packages" / "sdk-python" / "src"), str(Path(__file__).parent))
        ),
    }
    if database_url is not None:
        environment["BULK_DATABASE_URL"] = database_url
    processes = [
        subprocess.Popen(
            [sys.executable, __file__, str(data_dir), str(first), str(min(share, count - first))],
            env=environment,
            stderr=subprocess.PIPE,
        )
        for first in range(0, count, share)
    ]
    failures = []
    for process in processes:
        _, stderr = process.communicate(timeout=600)
        if process.returncode != 0:
            failures.append(stderr.decode("utf-8", "replace")[-2000:])
    if failures:
        raise RuntimeError("bulk ingest failed:\n" + "\n".join(failures))


def _main(data_dir: str, first: str, count: str) -> None:
    from earshot.storage import IncidentStore
    from incident_factory import make_valid_bundle
    from store_support import TEST_CORRELATION_KEY

    database_url = os.environ.get("BULK_DATABASE_URL")
    store = IncidentStore(
        data_dir,
        database_url=database_url,
        correlation_key=TEST_CORRELATION_KEY if database_url else None,
    )
    try:
        for index in range(int(first), int(first) + int(count)):
            store.ingest(
                make_valid_bundle(
                    bundle_id=small_incident_id(index),
                    include_raw_otlp=False,
                    include_render=False,
                )
            )
    finally:
        store.close()


if __name__ == "__main__":
    _main(*sys.argv[1:])
