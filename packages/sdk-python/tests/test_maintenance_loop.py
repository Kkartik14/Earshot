"""The server maintenance loop: one tick, every job, an injected clock."""

from __future__ import annotations

import asyncio
import logging

import pytest

from earshot.clock import ManualClock
from earshot.maintenance import MaintenanceLoop

pytestmark = pytest.mark.unit


def test_each_tick_runs_every_job_with_the_clocks_wall_time() -> None:
    clock = ManualClock(wall=1_000)
    loop = MaintenanceLoop(clock=clock)
    seen: list[tuple[str, int]] = []

    def recording(name: str):
        async def job(now_unix_nano: int) -> None:
            seen.append((name, now_unix_nano))

        return job

    loop.add_job("first", recording("first"))
    loop.add_job("second", recording("second"))

    asyncio.run(loop.run_once())
    clock.advance(500)
    asyncio.run(loop.run_once())

    assert sorted(seen) == [
        ("first", 1_000),
        ("first", 1_500),
        ("second", 1_000),
        ("second", 1_500),
    ]
    assert loop.job_names == ("first", "second")


def test_a_failing_job_is_logged_and_does_not_stop_the_others(caplog) -> None:
    loop = MaintenanceLoop(clock=ManualClock(wall=7))
    seen: list[int] = []

    async def broken(_now: int) -> None:
        raise RuntimeError("payload-free failure")

    async def healthy(now: int) -> None:
        seen.append(now)

    loop.add_job("broken", broken)
    loop.add_job("healthy", healthy)

    with caplog.at_level(logging.ERROR, logger="earshot.maintenance"):
        asyncio.run(loop.run_once())

    assert seen == [7]
    assert "maintenance job broken failed (RuntimeError)" in caplog.text


def test_run_ticks_until_stopped() -> None:
    loop = MaintenanceLoop(clock=ManualClock(), tick_seconds=0.001)
    ticks: list[int] = []

    async def scenario() -> None:
        stop = asyncio.Event()

        async def counting(now: int) -> None:
            ticks.append(now)
            if len(ticks) == 3:
                stop.set()

        loop.add_job("counting", counting)
        await asyncio.wait_for(loop.run(stop), timeout=5)

    asyncio.run(scenario())

    assert len(ticks) == 3


def test_job_names_are_unique_and_the_tick_is_positive() -> None:
    loop = MaintenanceLoop()

    async def job(_now: int) -> None:
        return None

    loop.add_job("only", job)
    with pytest.raises(ValueError, match="already registered"):
        loop.add_job("only", job)
    with pytest.raises(ValueError, match="positive"):
        MaintenanceLoop(tick_seconds=0)


def test_a_job_with_more_work_runs_again_at_once_without_rushing_the_others() -> None:
    loop = MaintenanceLoop(clock=ManualClock(), tick_seconds=30)
    draining: list[int] = []
    ticking: list[int] = []

    async def scenario() -> None:
        stop = asyncio.Event()

        async def drain(now: int) -> float | None:
            draining.append(now)
            if len(draining) == 5:
                stop.set()
            # A batch of a longer task: come back at once while work remains.
            return 0.0

        async def tick(now: int) -> None:
            ticking.append(now)

        loop.add_job("drain", drain)
        loop.add_job("tick", tick)
        await asyncio.wait_for(loop.run(stop), timeout=5)

    asyncio.run(scenario())

    assert len(draining) == 5
    assert len(ticking) == 1
