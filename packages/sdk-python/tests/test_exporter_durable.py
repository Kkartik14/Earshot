from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from earshot.exporter import (
    DurableExporter,
    ExportItem,
    HttpExportTransport,
    PermanentExportError,
    RetryableExportError,
    SynchronousExporter,
)

pytestmark = pytest.mark.unit

_STANDALONE_FINGERPRINT = hashlib.sha256(b"earshot.standalone").hexdigest()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        time.sleep(0.005)


class _Unavailable:
    def send(self, _item: ExportItem) -> None:
        raise RetryableExportError("unavailable")


class _Rejecting:
    def send(self, _item: ExportItem) -> None:
        raise PermanentExportError("rejected")


class _Recording:
    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def send(self, item: ExportItem) -> None:
        self.items.append(item)


def _durable(transport, spool_dir: Path, **kwargs) -> DurableExporter:
    return DurableExporter(
        transport,
        spool_dir=spool_dir,
        max_attempts=1,
        base_backoff=0,
        max_elapsed=0.1,
        **kwargs,
    )


def test_atomic_spool_is_private_and_contains_no_partial_file(tmp_path) -> None:
    exporter = _durable(_Unavailable(), tmp_path)
    try:
        assert exporter.submit(ExportItem("bundle/../opaque", b"exact\x00payload"))
        _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)

        spool_file = next(tmp_path.glob("*.spool"))
        assert stat.S_IMODE(spool_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert list(tmp_path.glob("*.tmp")) == []
        record = json.loads(spool_file.read_bytes())
        assert record["bundle_id"] == "bundle/../opaque"
        assert "/" not in spool_file.name
    finally:
        assert exporter.shutdown()


def test_restart_replays_exact_payload_content_type_and_idempotency_key(tmp_path) -> None:
    item = ExportItem("exact-id", b"\x00\xffpayload", "application/custom")
    first = _durable(_Unavailable(), tmp_path)
    assert first.submit(item)
    assert first.shutdown()

    transport = _Recording()
    second = _durable(transport, tmp_path)
    try:
        assert second.flush(2)
        assert transport.items == [item]
        assert second.status().replayed == 1
        assert second.status().spool_depth == 0
    finally:
        assert second.shutdown()


def test_corrupt_record_is_quarantined_and_observable(tmp_path) -> None:
    corrupt = tmp_path / f"{_STANDALONE_FINGERPRINT}-broken.spool"
    corrupt.write_bytes(b"not-json")
    os.chmod(corrupt, 0o600)
    diagnostics = []

    exporter = _durable(_Recording(), tmp_path, diagnostic=diagnostics.append)
    try:
        _wait_until(lambda: exporter.status().abandoned == 1)
        assert list(tmp_path.glob("*.spool")) == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
        assert diagnostics[-1].code == "exporter.spool_corrupt"
    finally:
        assert exporter.shutdown()


def test_startup_quarantines_crash_remnant_temp_file(tmp_path) -> None:
    temporary = tmp_path / f".{_STANDALONE_FINGERPRINT}-interrupted.tmp"
    temporary.write_bytes(b"partial")
    os.chmod(temporary, 0o600)
    diagnostics = []

    exporter = _durable(_Recording(), tmp_path, diagnostic=diagnostics.append)
    try:
        assert exporter.status().abandoned == 1
        assert not temporary.exists()
        assert diagnostics[-1].code == "exporter.spool_corrupt"
    finally:
        assert exporter.shutdown()


def test_spool_item_and_byte_caps_fail_open_with_overflow_status(tmp_path) -> None:
    diagnostics = []
    exporter = _durable(
        _Unavailable(),
        tmp_path,
        max_spool_items=1,
        max_spool_bytes=1024,
        diagnostic=diagnostics.append,
    )
    try:
        assert exporter.submit(ExportItem("first", b"one"))
        assert not exporter.submit(ExportItem("second", b"two"))
        status = exporter.status()
        assert status.dropped == 1
        assert status.overflow == 1
        assert any(item.code == "exporter.spool_full" for item in diagnostics)
    finally:
        assert exporter.shutdown()

    byte_limited = _durable(
        _Unavailable(),
        tmp_path / "bytes",
        max_spool_bytes=16,
    )
    try:
        assert not byte_limited.submit(ExportItem("large", b"too-large"))
        assert byte_limited.status().overflow == 1
    finally:
        assert byte_limited.shutdown()


def test_retryable_failure_retains_spool_record(tmp_path) -> None:
    exporter = _durable(_Unavailable(), tmp_path)
    try:
        assert exporter.submit(ExportItem("retry", b"payload"))
        _wait_until(lambda: exporter.status().retried > 0)
        assert exporter.status().failed == 0
        assert exporter.status().spool_depth == 1
        assert len(list(tmp_path.glob("*.spool"))) == 1
    finally:
        assert exporter.shutdown()


def test_cross_cycle_backoff_prevents_tight_retry_bursts(tmp_path) -> None:
    class CountingUnavailable:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, _item: ExportItem) -> None:
            self.calls += 1
            raise RetryableExportError("unavailable")

    transport = CountingUnavailable()
    exporter = _durable(transport, tmp_path)
    try:
        assert exporter.submit(ExportItem("backoff", b"payload"))
        _wait_until(lambda: transport.calls >= 2)
        time.sleep(0.18)
        assert transport.calls <= 4
    finally:
        assert exporter.shutdown()


def test_new_same_process_record_is_not_counted_as_restart_replay(tmp_path) -> None:
    transport = _Recording()
    exporter = _durable(transport, tmp_path)
    try:
        assert exporter.submit(ExportItem("new", b"payload"))
        assert exporter.flush(2)
        assert exporter.status().sent == 1
        assert exporter.status().replayed == 0
    finally:
        assert exporter.shutdown()


def test_destination_fingerprint_never_replays_another_routes_record(tmp_path) -> None:
    route_a = _durable(
        _Unavailable(),
        tmp_path,
        destination_fingerprint="a" * 64,
    )
    assert route_a.submit(ExportItem("route-a", b"private-a"))
    assert route_a.shutdown()

    transport_b = _Recording()
    route_b = _durable(
        transport_b,
        tmp_path,
        destination_fingerprint="b" * 64,
    )
    try:
        assert route_b.flush(1)
        assert transport_b.items == []
        assert route_b.status().spool_depth == 1
        assert route_b.status().abandoned == 1
        assert len(list(tmp_path.glob("a*.spool"))) == 1
        assert route_b.submit(ExportItem("route-b", b"private-b"))
        assert route_b.flush(2)
        assert [item.bundle_id for item in transport_b.items] == ["route-b"]
    finally:
        assert route_b.shutdown()


def test_spool_caps_apply_across_route_fingerprints(tmp_path) -> None:
    route_a = _durable(
        _Unavailable(),
        tmp_path,
        destination_fingerprint="a" * 64,
        max_spool_items=2,
    )
    assert route_a.submit(ExportItem("route-a", b"a"))
    assert route_a.shutdown()

    route_b = _durable(
        _Unavailable(),
        tmp_path,
        destination_fingerprint="b" * 64,
        max_spool_items=2,
    )
    try:
        assert route_b.submit(ExportItem("route-b", b"b"))
        assert not route_b.submit(ExportItem("route-b-overflow", b"c"))
        assert route_b.status().spool_depth == 2
        assert route_b.status().abandoned == 1
        assert route_b.status().overflow == 1
    finally:
        assert route_b.shutdown()


def test_symlink_spool_entry_is_never_read_or_sent(tmp_path) -> None:
    outside = tmp_path.parent / "outside-spool-target"
    outside.write_bytes(b"must-not-be-read")
    symlink = tmp_path / f"{_STANDALONE_FINGERPRINT}-symlink.spool"
    symlink.symlink_to(outside)
    transport = _Recording()

    exporter = _durable(transport, tmp_path)
    try:
        _wait_until(lambda: exporter.status().abandoned == 1)
        assert transport.items == []
        assert outside.read_bytes() == b"must-not-be-read"
    finally:
        assert exporter.shutdown()


@pytest.mark.parametrize("policy", ["retain", "delete"])
def test_permanent_rejection_follows_explicit_retention_policy(tmp_path, policy: str) -> None:
    exporter = _durable(
        _Rejecting(),
        tmp_path,
        permanent_rejection_policy=policy,
    )
    try:
        assert exporter.submit(ExportItem("rejected", b"payload"))
        _wait_until(lambda: exporter.status().rejected == 1)
        status = exporter.status()
        if policy == "retain":
            assert status.spool_depth == 1
            assert status.abandoned == 1
            assert len(list(tmp_path.glob("*.rejected"))) == 1
        else:
            assert status.spool_depth == 0
            assert status.abandoned == 0
            assert list(tmp_path.iterdir()) == []
    finally:
        assert exporter.shutdown()


def test_repeated_shutdown_is_idempotent(tmp_path) -> None:
    exporter = _durable(_Recording(), tmp_path)
    assert exporter.shutdown()
    assert exporter.shutdown()
    assert not exporter.submit(ExportItem("late", b"payload"))


def test_insecure_or_implicit_spool_storage_is_rejected(tmp_path) -> None:
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    os.chmod(insecure, 0o755)
    with pytest.raises(ValueError, match="group or other"):
        _durable(_Recording(), insecure)


def test_spool_write_failure_is_observable_and_fail_open(monkeypatch, tmp_path) -> None:
    diagnostics = []
    exporter = _durable(_Recording(), tmp_path, diagnostic=diagnostics.append)

    def fail_replace(_source, _destination) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        assert not exporter.submit(ExportItem("write-failure", b"payload"))
        status = exporter.status()
        assert status.dropped == 1
        assert status.failed == 1
        assert diagnostics[-1].code == "exporter.spool_write_failed"
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        assert exporter.shutdown()


def test_sync_retry_after_cannot_exceed_total_deadline() -> None:
    class DeadlineTransport:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def send_with_timeout(self, _item: ExportItem, *, timeout: float) -> None:
            self.timeouts.append(timeout)
            raise RetryableExportError("later", retry_after=60)

    transport = DeadlineTransport()
    exporter = SynchronousExporter(
        transport,
        max_attempts=3,
        base_backoff=0,
        max_elapsed=0.02,
    )
    started = time.monotonic()
    assert not exporter.submit(ExportItem("deadline", b"payload"))
    assert time.monotonic() - started < 0.2
    assert len(transport.timeouts) == 1
    assert exporter.status().failed == 1


def test_http_transport_sends_explicit_project_header() -> None:
    captured = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Opener:
        def open(self, request, *, timeout):
            captured.update(dict(request.header_items()))
            captured["timeout"] = timeout
            return Response()

    transport = HttpExportTransport(
        "http://localhost:4319",
        project_id="voice-production",
        compression_threshold_bytes=None,
    )
    transport._opener = Opener()  # type: ignore[assignment]
    transport.send(ExportItem("project", b"payload"))

    assert captured["X-earshot-project-id"] == "voice-production"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_inherited_durable_exporter_is_usable_after_fork(tmp_path) -> None:
    delivery_log = tmp_path / "delivered.log"

    class FileTransport:
        def send(self, item: ExportItem) -> None:
            descriptor = os.open(delivery_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, item.bundle_id.encode() + b"\n")
            finally:
                os.close(descriptor)

    exporter = _durable(FileTransport(), tmp_path / "spool")
    child = os.fork()
    if child == 0:
        try:
            accepted = exporter.submit(ExportItem("child-bundle", b"payload"))
            flushed = exporter.flush(2)
            stopped = exporter.shutdown(2)
            os._exit(0 if accepted and flushed and stopped else 1)
        except BaseException:
            os._exit(2)

    _, wait_status = os.waitpid(child, 0)
    try:
        assert os.waitstatus_to_exitcode(wait_status) == 0
        _wait_until(lambda: delivery_log.exists())
        assert b"child-bundle\n" in delivery_log.read_bytes()
    finally:
        assert exporter.shutdown()
