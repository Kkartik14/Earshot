"""Real processes that hold a store's locks, for lock-budget tests.

Each holder takes one lock in its own process, prints ``held`` once it has it,
and keeps it until its stdin closes. Nothing is simulated: the store under test
meets the same contention another Earshot process, a CLI writer or a SQLite
``VACUUM`` would cause.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from store_support import current_dialect, postgres_cluster

ROOT = Path(__file__).resolve().parents[3]

FILE_LOCK_HOLDER = """
import fcntl, sys
handle = open(sys.argv[1], "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""
K_WRITE_HOLDER = """
import os, sys, psycopg
from earshot.storage_postgres import K_WRITE
connection = psycopg.connect(os.environ["HOLDER_DATABASE_URL"], autocommit=True)
connection.execute("SELECT pg_advisory_lock(%s)", (K_WRITE,))
print("held", flush=True)
sys.stdin.read()
"""
SQLITE_WRITER_HOLDER = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
print("held", flush=True)
sys.stdin.read()
"""
# A real VACUUM, paused part-way: SQLite calls the progress handler while the
# VACUUM runs; once a second connection can no longer take the write lock, the
# handler reports it and blocks until stdin closes, then the VACUUM completes.
SQLITE_VACUUM_HOLDER = """
import sqlite3, sys
path = sys.argv[1]
vacuum = sqlite3.connect(path, isolation_level=None)
probe = sqlite3.connect(path, isolation_level=None, timeout=0)
state = {"held": False}

def progress():
    if state["held"]:
        return 0
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    except sqlite3.OperationalError:
        state["held"] = True
        print("held", flush=True)
        sys.stdin.read()
    return 0

vacuum.set_progress_handler(progress, 1)
vacuum.execute("VACUUM")
assert state["held"], "the VACUUM never held the write lock"
"""


@contextlib.contextmanager
def held_by_another_process(script: str, *arguments: str, **environment: str) -> Iterator[None]:
    holder = subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
            **environment,
        },
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == b"held\n"
        yield
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        assert holder.wait(timeout=30) == 0


@contextlib.contextmanager
def store_write_lock_held_elsewhere(path: Path) -> Iterator[None]:
    """Another process holds this store's cross-process write lock: ``.store.lock``
    on SQLite, ``K_write`` on Postgres."""

    if current_dialect() == "sqlite":
        with held_by_another_process(FILE_LOCK_HOLDER, str(path / ".store.lock")):
            yield
        return
    with held_by_another_process(
        K_WRITE_HOLDER, HOLDER_DATABASE_URL=postgres_cluster().database_for(path)
    ):
        yield
