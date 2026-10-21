"""Leased jobs in a catalog table: claim, lease, fence, retry with backoff, give up.

A job table holds one row per job, named by its key columns, with the columns this
module reads and writes:

- ``state``: ``pending`` (waiting), ``running`` (leased) or a terminal state the
  owner names (for example ``stored``, ``failed``, ``unavailable``);
- ``attempts``: failed attempts so far;
- ``lease_until_unix_nano``: when a running job's lease expires;
- ``lease_token``: bumped by every claim, so a holder whose lease was taken over
  writes nothing (every write below is conditional on it);
- ``next_attempt_unix_nano``: when a retried job may run again (``NULL``: now);
- ``error_code``: the last stable error code;
- ``updated_unix_nano``.

**Claim** (inside the caller's write transaction): the first due job, that is a
running job whose lease expired (its holder died) or a pending job whose
``next_attempt`` has passed, becomes ``running`` under a new token and a lease the
caller sizes per job. **Complete** writes a terminal state and the caller's columns
only while the claim's lease token still holds the row. **Retry** counts a failed
attempt and puts the job off by the next delay of ``retry_delays_seconds``; past the
last one it becomes terminal ``failed`` with the last code. **Release** returns
every running job to pending: at a server's start no holder survives (one server
owns a catalog).

The helper holds no SQL dialect beyond what SQLite and Postgres share, and takes the
caller's connection: the caller owns the transaction, the write lock and the
mutation budget, as the call closer does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage_database import DatabaseConnection

_SECOND = 1_000_000_000
PENDING = "pending"
RUNNING = "running"
FAILED = "failed"
# A failed attempt waits 1, 4 and 16 minutes; the fourth failure is terminal.
DEFAULT_RETRY_DELAYS_SECONDS = (60, 240, 960)


@dataclass(frozen=True, slots=True)
class JobLease:
    """One claimed job: its key, the claim's token, its lease and its prior failures."""

    key: tuple[Any, ...]
    token: int
    lease_until_unix_nano: int
    attempts: int


@dataclass(frozen=True, slots=True)
class LeasedJobs:
    """The leased jobs of one table, keyed by ``key_columns``."""

    table: str
    key_columns: tuple[str, ...]
    retry_delays_seconds: tuple[int, ...] = DEFAULT_RETRY_DELAYS_SECONDS

    @property
    def _key_match(self) -> str:
        return " AND ".join(f"{column} = ?" for column in self.key_columns)

    @property
    def _key_list(self) -> str:
        return ", ".join(self.key_columns)

    def due(self, connection: DatabaseConnection, now: int, *, limit: int = 1) -> list[tuple]:
        """Keys of up to ``limit`` due jobs: expired leases first, then pending jobs
        never put off, then those whose backoff has passed, earliest first. Each read
        is a range of the ``(state, lease_until)`` and ``(state, next_attempt)``
        indexes a job table keeps; none sorts the whole queue."""

        keys: list[tuple] = []
        for where, parameters, order in (
            (
                f"state = '{RUNNING}' AND lease_until_unix_nano < ?",
                (now,),
                "ORDER BY lease_until_unix_nano",
            ),
            (f"state = '{PENDING}' AND next_attempt_unix_nano IS NULL", (), ""),
            (
                f"state = '{PENDING}' AND next_attempt_unix_nano <= ?",
                (now,),
                "ORDER BY next_attempt_unix_nano",
            ),
        ):
            if len(keys) >= limit:
                break
            rows = connection.execute(
                f"SELECT {self._key_list} FROM {self.table} WHERE {where} {order} LIMIT ?",
                (*parameters, limit - len(keys)),
            ).fetchall()
            keys.extend(tuple(row[column] for column in self.key_columns) for row in rows)
        return keys

    def claim(
        self, connection: DatabaseConnection, now: int, *, lease_nano: Callable[[tuple], int]
    ) -> JobLease | None:
        """Lease the first due job for ``lease_nano(key)`` nanoseconds, or ``None``."""

        for key in self.due(connection, now):
            lease_until = now + lease_nano(key)
            claimed = connection.execute(
                f"UPDATE {self.table} SET state = '{RUNNING}', lease_token = lease_token + 1, "
                "lease_until_unix_nano = ?, updated_unix_nano = ? "
                f"WHERE {self._key_match} AND ((state = '{RUNNING}' AND "
                f"lease_until_unix_nano < ?) OR (state = '{PENDING}' AND "
                "(next_attempt_unix_nano IS NULL OR next_attempt_unix_nano <= ?)))",
                (lease_until, now, *key, now, now),
            ).rowcount
            if claimed != 1:
                continue
            row = connection.execute(
                f"SELECT lease_token, attempts FROM {self.table} WHERE {self._key_match}", key
            ).fetchone()
            return JobLease(key, int(row["lease_token"]), lease_until, int(row["attempts"]))
        return None

    def holds(self, connection: DatabaseConnection, lease: JobLease) -> bool:
        """Whether ``lease`` still holds its job (running, under the claim's token)."""

        return (
            connection.execute(
                f"SELECT 1 FROM {self.table} WHERE {self._key_match} AND state = '{RUNNING}' "
                "AND lease_token = ?",
                (*lease.key, lease.token),
            ).fetchone()
            is not None
        )

    def complete(
        self,
        connection: DatabaseConnection,
        lease: JobLease,
        now: int,
        *,
        state: str,
        error_code: str | None = None,
        columns: dict[str, Any] | None = None,
    ) -> bool:
        """End the leased job in the terminal ``state``; ``False`` if the lease was lost."""

        assignments = dict(columns or {})
        names = "".join(f", {name} = ?" for name in assignments)
        return (
            connection.execute(
                f"UPDATE {self.table} SET state = ?, error_code = ?, lease_until_unix_nano = NULL, "
                f"next_attempt_unix_nano = NULL, updated_unix_nano = ?{names} "
                f"WHERE {self._key_match} AND state = '{RUNNING}' AND lease_token = ?",
                (state, error_code, now, *assignments.values(), *lease.key, lease.token),
            ).rowcount
            == 1
        )

    def retry(
        self,
        connection: DatabaseConnection,
        lease: JobLease,
        now: int,
        *,
        error_code: str,
        columns: dict[str, Any] | None = None,
    ) -> str | None:
        """Count a failed attempt: back to ``pending`` after its delay, or terminal
        ``failed`` past the last delay. Returns the new state (``None``: lease lost)."""

        attempts = lease.attempts + 1
        delays = self.retry_delays_seconds
        if attempts > len(delays):
            state, next_attempt = FAILED, None
        else:
            state, next_attempt = PENDING, now + delays[attempts - 1] * _SECOND
        assignments = dict(columns or {}) if state == FAILED else {}
        names = "".join(f", {name} = ?" for name in assignments)
        updated = connection.execute(
            f"UPDATE {self.table} SET state = ?, attempts = ?, error_code = ?, "
            "lease_until_unix_nano = NULL, next_attempt_unix_nano = ?, "
            f"updated_unix_nano = ?{names} "
            f"WHERE {self._key_match} AND state = '{RUNNING}' AND lease_token = ?",
            (
                state,
                attempts,
                error_code,
                next_attempt,
                now,
                *assignments.values(),
                *lease.key,
                lease.token,
            ),
        ).rowcount
        return state if updated == 1 else None

    def release(self, connection: DatabaseConnection, now: int) -> int:
        """Return every running job to ``pending``, due now (a server's start)."""

        return int(
            connection.execute(
                f"UPDATE {self.table} SET state = '{PENDING}', lease_until_unix_nano = NULL, "
                f"next_attempt_unix_nano = NULL, updated_unix_nano = ? WHERE state = '{RUNNING}'",
                (now,),
            ).rowcount
        )
