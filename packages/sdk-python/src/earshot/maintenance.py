"""The server's maintenance loop: small periodic jobs that run in the app's lifespan.

Every registered job runs once a second (``TICK_SECONDS``). A job is an async
callable that receives the tick's wall time from the loop's clock, so tests drive
it with a ``ManualClock`` and ``run_once()`` instead of sleeping. A failing job is
logged and does not stop the loop or the other jobs.

Each job keeps its own cadence. A job that returns a number of seconds runs again
after that delay instead of the tick, so a long task can work through bounded
batches back to back (returning ``0``) while a slow batch never delays another
job's tick.

A job that holds something for the server's lifetime (the call closer's
materializer process) has an async ``aclose()``; the loop calls it as soon as it
is told to stop, while a run in progress finishes, so the app's lifespan shutdown
releases it under every entry point. ``aclose`` must also bound that run: the
closer's gives a revision build a short grace, then kills its child, so a stalled
child can never hold up shutdown.

The first job is the Postgres server-lock watchdog (``database_lock_watchdog``):
it probes the ``K_server`` connection every tick and, once the lock is gone,
logs ``EARSHOT_DATABASE_LOCK_LOST`` and exits the process with status 70. An
exception in a lifespan task does not stop uvicorn, so the exit is explicit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .clock import Clock, SystemClock

_LOGGER = logging.getLogger(__name__)

TICK_SECONDS = 1.0
# A probe on an idle, keepalive-protected connection that has not answered in
# this long means the link is gone (the keepalives give up after about 8 s).
LOCK_PROBE_TIMEOUT_SECONDS = 10.0
LOCK_LOST_EXIT_CODE = 70
# Maintenance jobs' mutations give way to request traffic: past this wait for the
# store's locks they give up and try again on a later tick.
MAINTENANCE_LOCK_BUDGET_SECONDS = 1.0
# A reprojection pass that found only incidents it cannot project waits this long
# before it tries them again (an operator may restore the artifacts meanwhile).
FAILED_REPROJECTION_RETRY_SECONDS = 60.0

# A job returns ``None`` to run again on the next tick, or the seconds to wait
# before it runs again (``0`` when it has more work queued).
MaintenanceJob = Callable[[int], Awaitable[float | None]]


class MaintenanceLoop:
    """Run registered jobs once per tick until stopped."""

    def __init__(self, *, clock: Clock | None = None, tick_seconds: float = TICK_SECONDS) -> None:
        if tick_seconds <= 0:
            raise ValueError("the maintenance tick must be positive")
        self.clock = clock or SystemClock()
        self.tick_seconds = tick_seconds
        self._jobs: list[tuple[str, MaintenanceJob]] = []
        self._closed = False

    @property
    def job_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._jobs)

    def add_job(self, name: str, job: MaintenanceJob) -> None:
        if name in self.job_names:
            raise ValueError(f"maintenance job {name!r} is already registered")
        self._jobs.append((name, job))

    async def run_once(self) -> None:
        """Run every job once, concurrently, with this tick's wall time."""

        now_unix_nano = self.clock.unix_nano()
        await asyncio.gather(*(self._run_job(name, job, now_unix_nano) for name, job in self._jobs))

    async def run(self, stop: asyncio.Event) -> None:
        """Run every job on its own cadence until ``stop`` is set, then close them.

        Each job's first run is immediate. Once ``stop`` is set no job starts a new
        run, and the jobs are closed at once, while a run in progress finishes: a
        job's ``aclose`` bounds its own run, so shutdown never waits on a stalled
        one (tribunal round 2, Lamport LAM2-2).
        """

        async def close_when_stopped() -> None:
            await stop.wait()
            await self.aclose()

        try:
            await asyncio.gather(
                *(self._run_job_until(stop, name, job) for name, job in self._jobs),
                close_when_stopped(),
            )
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Close every job that has an ``aclose``, once (a failure is logged, not raised)."""

        if self._closed:
            return
        self._closed = True
        for name, job in self._jobs:
            close = getattr(job, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as error:
                _LOGGER.error("maintenance job %s failed to close (%s)", name, type(error).__name__)

    async def _run_job_until(self, stop: asyncio.Event, name: str, job: MaintenanceJob) -> None:
        while not stop.is_set():
            delay = await self._run_job(name, job, self.clock.unix_nano())
            timeout = self.tick_seconds if delay is None else max(0.0, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=timeout)

    @staticmethod
    async def _run_job(name: str, job: MaintenanceJob, now_unix_nano: int) -> float | None:
        try:
            return await job(now_unix_nano)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error("maintenance job %s failed (%s)", name, type(error).__name__)
            return None


class ServerLock(Protocol):
    """A lock a server holds for its lifetime; ``verify`` raises once it is lost."""

    def verify(self) -> None: ...

    def release(self) -> None: ...


def exit_on_lost_database_lock(reason: str) -> None:
    """Log the stable code and leave at once: another server may own the database now."""

    _LOGGER.critical(
        "EARSHOT_DATABASE_LOCK_LOST: %s; exiting so this process cannot write beside "
        "another server",
        reason,
    )
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    os._exit(LOCK_LOST_EXIT_CODE)


def database_lock_watchdog(
    lock: ServerLock,
    *,
    on_lost: Callable[[str], None] = exit_on_lost_database_lock,
    probe_timeout_seconds: float = LOCK_PROBE_TIMEOUT_SECONDS,
) -> MaintenanceJob:
    """A maintenance job that probes ``lock`` each tick and calls ``on_lost`` on loss."""

    async def watch(_now_unix_nano: int) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(lock.verify), timeout=probe_timeout_seconds)
        except TimeoutError:
            on_lost("the server lock connection did not answer")
        except Exception as error:
            on_lost(str(error) or type(error).__name__)

    return watch


class ProjectionStore(Protocol):
    """The part of the incident store the reprojection job drives."""

    def projections_pending(self) -> int: ...

    def reproject_stale(self, *, limit: int, after: str | None = None) -> Any: ...


def reprojection_job(store: ProjectionStore, *, batch_size: int = 100) -> MaintenanceJob:
    """A maintenance job that re-projects stale incidents after the server starts.

    Each run is one bounded ``reproject_stale`` batch in a worker thread (its
    writes are one short mutation). While a pass over the stale incidents has
    more batches, the job asks to run again at once; an idle job costs one
    indexed count per tick. A pass that only met incidents it cannot project
    backs off for ``FAILED_REPROJECTION_RETRY_SECONDS``.
    """

    resume_after: str | None = None
    pass_reprojected = 0
    pass_failed = 0

    async def reproject(_now_unix_nano: int) -> float | None:
        nonlocal resume_after, pass_reprojected, pass_failed
        if resume_after is None:
            pending = await asyncio.to_thread(store.projections_pending)
            if pending == 0:
                return None
        batch = await asyncio.to_thread(store.reproject_stale, limit=batch_size, after=resume_after)
        resume_after = batch.resume_after
        pass_reprojected += batch.reprojected
        pass_failed += len(batch.failed)
        if resume_after is not None:
            return 0.0
        only_failures = pass_failed > 0 and pass_reprojected == 0
        pass_reprojected = pass_failed = 0
        return FAILED_REPROJECTION_RETRY_SECONDS if only_failures else None

    return reproject
