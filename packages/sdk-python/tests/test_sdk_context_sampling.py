from __future__ import annotations

import asyncio
import os
import select
import threading

import pytest

import earshot
from earshot.exporter import ExportItem, HttpExportTransport
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_global_sdk_configuration():
    earshot.shutdown()
    earshot.configure()
    yield
    earshot.shutdown()
    earshot.configure()


def test_nested_conversations_and_operations_restore_their_owner_context() -> None:
    first = earshot.Client(project_id="project-a")
    second = earshot.Client(project_id="project-b")
    assert earshot.current_context() is None

    with first.conversation(session_id="conversation-a") as recorder:
        outer = earshot.current_context()
        assert outer is not None
        assert outer.client_id == first.client_id
        assert outer.project_id == "project-a"
        assert outer.conversation_id == "conversation-a"
        assert outer.operation_id is None
        with recorder.operation("llm", operation_id="operation-a"):
            assert earshot.current_operation() == "operation-a"
        assert earshot.current_operation() is None

        with second.conversation(session_id="conversation-b"):
            nested = earshot.current_context()
            assert nested is not None
            assert nested.client_id == second.client_id
            assert nested.project_id == "project-b"
            assert nested.conversation_id == "conversation-b"

        assert earshot.current_context() == outer

    assert earshot.current_context() is None


def test_async_conversation_context_is_task_local_and_nesting_restores_tokens() -> None:
    client = earshot.Client(project_id="async-project")

    async def exercise() -> None:
        async with client.conversation(session_id="async-outer"):
            assert earshot.current_conversation() == "async-outer"

            async def inherited_task() -> str | None:
                await asyncio.sleep(0)
                return earshot.current_conversation()

            assert await asyncio.create_task(inherited_task()) == "async-outer"
            async with client.conversation(session_id="async-inner"):
                assert earshot.current_conversation() == "async-inner"
            assert earshot.current_conversation() == "async-outer"
        assert earshot.current_context() is None

    asyncio.run(exercise())


def test_new_threads_start_without_conversation_context() -> None:
    client = earshot.Client(project_id="thread-project")
    observed: list[object] = []
    with client.conversation(session_id="main-thread"), earshot.suppress_instrumentation():
        thread = threading.Thread(
            target=lambda: observed.append(
                (earshot.current_context(), earshot.is_instrumentation_suppressed())
            )
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert earshot.current_conversation() == "main-thread"
        assert earshot.is_instrumentation_suppressed()
    assert observed == [(None, False)]


def test_conversation_context_restores_after_sync_and_async_exceptions() -> None:
    client = earshot.Client(project_id="exception-project")
    with (
        pytest.raises(RuntimeError, match="sync failure"),
        client.conversation(session_id="sync-failure"),
    ):
        raise RuntimeError("sync failure")
    assert earshot.current_context() is None

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="async failure"):
            async with client.conversation(session_id="async-failure"):
                raise RuntimeError("async failure")
        assert earshot.current_context() is None

    asyncio.run(exercise())


def test_suppression_is_nested_and_task_local() -> None:
    assert not earshot.is_instrumentation_suppressed()
    with earshot.suppress_instrumentation():
        assert earshot.is_instrumentation_suppressed()
        with earshot.suppress_instrumentation():
            assert earshot.is_instrumentation_suppressed()
        assert earshot.is_instrumentation_suppressed()
    assert not earshot.is_instrumentation_suppressed()

    async def exercise() -> None:
        async with earshot.suppress_instrumentation():
            assert earshot.is_instrumentation_suppressed()
            assert await asyncio.create_task(_suppression_value())
        assert not earshot.is_instrumentation_suppressed()

    async def _suppression_value() -> bool:
        return earshot.is_instrumentation_suppressed()

    asyncio.run(exercise())


def test_root_sampling_drops_or_keeps_the_entire_incident() -> None:
    dropped = earshot.Client(
        endpoint="http://localhost:4319",
        project_id="sampling",
        sampling_rate=0.0,
    )
    recorder = dropped.session(session_id="drop-entire-root")
    with recorder.operation("llm", operation_id="kept-in-local-artifact"):
        recorder.record_event("llm.first_token")
    bundle = recorder.close()
    assert len(bundle.profile.operations) == 1
    assert len(bundle.profile.events) == 1
    assert recorder.export_accepted is None
    dropped_status = dropped.status()
    assert dropped_status.sampled_conversations == 0
    assert dropped_status.unsampled_conversations == 1
    assert dropped_status.last_sampling_reason == "dropped_by_root_rate"
    assert dropped_status.lost == 0

    kept = earshot.Client(project_id="sampling", sampling_rate=1.0)
    kept.session(session_id="keep-entire-root").close()
    assert kept.status().sampled_conversations == 1
    assert kept.status().unsampled_conversations == 0


def test_root_sampling_is_deterministic_across_clients_with_the_same_project_seed() -> None:
    first = earshot.Client(project_id="stable", sampling_rate=0.5, sampling_seed="seed")
    second = earshot.Client(project_id="stable", sampling_rate=0.5, sampling_seed="seed")
    first_decisions = [
        first.sampling_decision(f"conversation-{index}").sampled for index in range(32)
    ]
    second_decisions = [
        second.sampling_decision(f"conversation-{index}").sampled for index in range(32)
    ]
    assert first_decisions == second_decisions
    assert any(first_decisions)
    assert not all(first_decisions)


def test_suppressed_conversation_is_not_exported_or_counted_as_delivery_loss() -> None:
    client = earshot.Client(endpoint="http://localhost:4319", sampling_rate=1.0)
    with earshot.suppress_instrumentation():
        client.session(session_id="recursive-http").close()
    status = client.status()
    assert status.suppressed_conversations == 1
    assert status.sampled_conversations == 0
    assert status.lost == 0
    assert status.last_sampling_reason == "instrumentation_suppressed"


def test_global_init_reads_earshot_environment_without_exposing_secrets(monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_ENDPOINT", "http://localhost:4319")
    monkeypatch.setenv("EARSHOT_TOKEN", SECRET_SENTINEL)
    monkeypatch.setenv("EARSHOT_PROJECT_ID", "environment-project")
    monkeypatch.setenv("EARSHOT_QUEUE_CAPACITY", "17")
    monkeypatch.setenv("EARSHOT_MAX_QUEUE_BYTES", "4096")
    monkeypatch.setenv("EARSHOT_COMPRESSION_THRESHOLD_BYTES", "2048")
    monkeypatch.setenv("EARSHOT_SAMPLING_RATE", "0")
    monkeypatch.setenv("EARSHOT_SAMPLING_SEED", "environment-seed")

    first = earshot.init()
    second = earshot.init()
    assert first is second
    assert first.config.endpoint == "http://localhost:4319"
    assert first.config.project_id == "environment-project"
    assert first.config.queue_capacity == 17
    assert first.config.max_queue_bytes == 4096
    assert first.config.compression_threshold_bytes == 2048
    assert first.config.sampling_rate == 0
    assert not hasattr(first.config, "token")
    assert SECRET_SENTINEL not in repr(first)
    assert SECRET_SENTINEL not in repr(first.config)


def test_invalid_environment_configuration_names_the_variable_not_its_values(monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_TOKEN", SECRET_SENTINEL)
    monkeypatch.setenv("EARSHOT_QUEUE_CAPACITY", "not-an-integer")
    with pytest.raises(ValueError, match="EARSHOT_QUEUE_CAPACITY") as raised:
        earshot.init()
    assert SECRET_SENTINEL not in str(raised.value)


def test_environment_can_explicitly_disable_export_compression(monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_COMPRESSION_THRESHOLD_BYTES", "off")
    assert earshot.init().config.compression_threshold_bytes is None


def test_http_export_runs_inside_instrumentation_suppression(monkeypatch) -> None:
    observed: list[bool] = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    transport = HttpExportTransport("http://localhost:4319")

    def open_request(*_args, **_kwargs):
        observed.append(earshot.is_instrumentation_suppressed())
        return Response()

    monkeypatch.setattr(transport._opener, "open", open_request)
    transport.send(ExportItem("suppressed-export", b"payload"))
    assert observed == [True]
    assert not earshot.is_instrumentation_suppressed()


def test_pipeline_recorder_also_blocks_cross_project_reconfiguration() -> None:
    earshot.configure(project_id="project-a")
    pipeline = earshot.pipeline(session_id="active-pipeline")
    with pytest.raises(RuntimeError, match="active recorder"):
        earshot.configure(project_id="project-b")
    pipeline.close()
    assert earshot.configure(project_id="project-b").project_id == "project-b"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_gets_fresh_client_ownership_and_no_parent_recorders() -> None:
    client = earshot.Client(project_id="fork-project")
    parent_client_id = client.client_id
    parent_recorder = client.session(session_id="parent-active")
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in the parent
        try:
            os.close(read_fd)
            with client.conversation(session_id="child-conversation"):
                child_context = earshot.current_context()
                assert child_context is not None
                result = f"{client.client_id},{child_context.client_id}"
            os.write(write_fd, result.encode())
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        readable, _, _ = select.select([read_fd], [], [], 5)
        assert readable, "forked child did not establish a fresh client context"
        child_client_id, context_client_id = os.read(read_fd, 256).decode().split(",")
        assert child_client_id == context_client_id
        assert child_client_id != parent_client_id
        _, wait_status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(wait_status) == 0
    finally:
        os.close(read_fd)
        parent_recorder.close()
        client.shutdown()
