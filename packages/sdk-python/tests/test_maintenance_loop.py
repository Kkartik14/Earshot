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


def test_instrumented_loop_exports_bounded_job_failure_and_schedule_metrics() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.test"))
    loop = MaintenanceLoop(clock=ManualClock(), tick_seconds=0.001, metrics=metrics)

    async def scenario() -> None:
        stop = asyncio.Event()
        runs = 0

        async def broken(_now: int) -> None:
            nonlocal runs
            runs += 1
            if runs == 2:
                stop.set()
            raise RuntimeError("sensitive exception details")

        loop.add_job("call-closer", broken)
        await asyncio.wait_for(loop.run(stop), timeout=5)

    asyncio.run(scenario())
    data = reader.get_metrics_data()
    observed = {
        metric.name: metric.data.data_points
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }

    failure_points = observed["earshot.maintenance.job.failures"]
    assert sum(point.value for point in failure_points) == 2
    assert {tuple(sorted(point.attributes.items())) for point in failure_points} == {
        (("error.type", "other"), ("job.name", "call-closer"))
    }
    assert sum(point.count for point in observed["earshot.maintenance.job.duration"]) == 2
    assert sum(point.count for point in observed["earshot.maintenance.scheduler.wakeup_delay"]) >= 2
    assert all(
        "sensitive exception details" not in str(point.attributes) for point in failure_points
    )
    provider.shutdown()


def test_scheduler_wakeup_delay_measures_lateness_against_completion_relative_deadline() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.scheduler-test"))
    clock = ManualClock()
    loop = MaintenanceLoop(clock=clock, tick_seconds=0.01, metrics=metrics)

    async def scenario() -> None:
        stop = asyncio.Event()
        runs = 0

        async def job(_now: int) -> None:
            nonlocal runs
            runs += 1
            if runs == 2:
                stop.set()

        async def advance_clock_after_the_first_deadline_is_scheduled() -> None:
            await asyncio.sleep(0.001)
            clock.advance(100_000_000)

        loop.add_job("call-closer", job)
        advance_clock_task = asyncio.create_task(
            advance_clock_after_the_first_deadline_is_scheduled()
        )
        await asyncio.wait_for(loop.run(stop), timeout=5)
        await advance_clock_task

    asyncio.run(scenario())
    data = reader.get_metrics_data()
    points = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "earshot.maintenance.scheduler.wakeup_delay"
        for point in metric.data.data_points
    ]
    assert sum(point.sum for point in points) >= 0.08
    provider.shutdown()


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
