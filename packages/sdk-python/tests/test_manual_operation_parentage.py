"""Nested manual operations must carry real OTel parentage.

Before this fix ``recorder.operation()`` minted a fresh span but never set a
parent, so a nested operation became a sibling/root and the analyzer's parent
walk orphaned it out of its enclosing turn.
"""

from __future__ import annotations

import asyncio

import pytest

from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.recorder import IncidentRecorder, RecorderConfig

pytestmark = pytest.mark.unit


def _recorder() -> IncidentRecorder:
    return IncidentRecorder(config=RecorderConfig(clock_domain_id="server-clock"))


def _by_name(bundle, name):
    return next(op for op in bundle.profile.operations if op.operation_name == name)


def test_nested_manual_operation_is_child_of_enclosing_span() -> None:
    rec = _recorder()
    with rec.operation("agent", turn_id="turn-1") as outer, rec.operation("tool") as inner:
        outer_span = outer["span_id"]
        inner_span = inner["span_id"]
    bundle = rec.close()

    tool = _by_name(bundle, "tool")
    agent = _by_name(bundle, "agent")
    assert tool.span_id == inner_span
    assert agent.span_id == outer_span
    assert tool.parent_span_id == outer_span
    assert tool.trace_id == agent.trace_id
    assert tool.parent_scope == "internal"


def test_nested_operation_inherits_turn_via_parent_walk() -> None:
    rec = _recorder()
    with rec.operation("agent", turn_id="turn-1"), rec.operation("tool"):
        pass
    bundle = rec.close()

    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    turn = analysis.projections.turns[0]
    tool = _by_name(bundle, "tool")
    assert tool.turn_id is None  # not set explicitly...
    assert tool.operation_id in turn.operation_ids  # ...but inherited via the parent walk


def test_async_nested_manual_operation_parentage() -> None:
    rec = _recorder()

    async def run() -> None:
        with rec.operation("agent", turn_id="turn-1"):
            await asyncio.sleep(0)
            with rec.operation("tool"):
                await asyncio.sleep(0)

    asyncio.run(run())
    bundle = rec.close()
    tool = _by_name(bundle, "tool")
    agent = _by_name(bundle, "agent")
    assert tool.parent_span_id == agent.span_id
    assert tool.parent_scope == "internal"


def test_nested_parentage_survives_inner_exception_and_siblings() -> None:
    rec = _recorder()
    with rec.operation("agent", turn_id="turn-1") as outer:
        outer_span = outer["span_id"]
        with pytest.raises(RuntimeError, match="boom"), rec.operation("tool"):
            raise RuntimeError("boom")
        with rec.operation("stt"):
            pass
    bundle = rec.close()

    assert _by_name(bundle, "tool").parent_span_id == outer_span
    assert _by_name(bundle, "tool").status == "error"
    assert _by_name(bundle, "stt").parent_span_id == outer_span


def test_concurrent_nested_operations_are_siblings_under_parent() -> None:
    rec = _recorder()

    async def run() -> str:
        with rec.operation("agent", turn_id="turn-1") as outer:

            async def child(i: int) -> None:
                with rec.operation("tool", operation_id=f"tool-{i}"):
                    await asyncio.sleep(0)

            await asyncio.gather(*(child(i) for i in range(4)))
            return outer["span_id"]

    outer_span = asyncio.run(run())
    bundle = rec.close()

    tools = [op for op in bundle.profile.operations if op.operation_name == "tool"]
    assert len(tools) == 4
    assert {t.parent_span_id for t in tools} == {outer_span}
    assert len({t.span_id for t in tools}) == 4  # distinct spans


def test_cross_recorder_nesting_does_not_forge_parent() -> None:
    outer_rec = _recorder()
    inner_rec = _recorder()
    with outer_rec.operation("agent", turn_id="turn-1"), inner_rec.operation("tool"):
        pass
    outer_bundle = outer_rec.close()
    inner_bundle = inner_rec.close()

    tool = _by_name(inner_bundle, "tool")
    # Different recorder -> different trace -> no forged parent across traces.
    assert tool.parent_span_id is None
    assert tool.parent_scope == "unknown"
    assert _by_name(outer_bundle, "agent").parent_span_id is None


def test_sequential_operations_are_not_parented() -> None:
    rec = _recorder()
    with rec.operation("stt", turn_id="turn-1"):
        pass
    with rec.operation("llm", turn_id="turn-1"):
        pass
    bundle = rec.close()

    assert _by_name(bundle, "stt").parent_span_id is None
    assert _by_name(bundle, "llm").parent_span_id is None
