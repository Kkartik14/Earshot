from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import earshot.storage as storage_module
from earshot.analysis import analyze_incident
from earshot.codec import decode_incident_protobuf, encode_incident_protobuf
from earshot.contract import DerivedAnalysis, ExportPolicy, RetentionPolicy
from earshot.storage import (
    ArtifactCorruptionError,
    IncidentConflictError,
    IncidentNotFoundError,
    IncidentPurgedError,
    IncidentStore,
    InvalidCursorError,
    StorageError,
)
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.integration


def canonical(bundle) -> bytes:
    return encode_incident_protobuf(bundle)


def analysis_value(
    store: IncidentStore,
    bundle_id: str,
    version: str,
    marker: str = "test_projection",
) -> DerivedAnalysis:
    record, payload = store.get_artifact(bundle_id)
    analysis = analyze_incident(
        decode_incident_protobuf(payload),
        input_sha256=record.digest,
        generated_at_unix_nano="1800000000000000000",
    )
    projections = analysis.projections.model_copy(
        update={"limitations": (*analysis.projections.limitations, marker)}
    )
    return analysis.model_copy(
        update={
            "analyzer_name": "test.analyzer",
            "analyzer_version": version,
            "projections": projections,
        }
    )


def with_status(bundle, status: str):
    session = bundle.profile.session.model_copy(update={"status": status})
    profile = bundle.profile.model_copy(update={"session": session})
    return bundle.model_copy(update={"profile": profile})


def with_metadata_retention(bundle, retention: RetentionPolicy):
    policies = tuple(
        policy.model_copy(update={"retention": retention})
        if policy.capture_class == "metadata"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def test_ingest_get_and_exact_retry_are_immutable_and_idempotent(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    payload = canonical(valid_bundle)
    first = store.ingest(valid_bundle, payload)
    second = store.ingest(valid_bundle, payload)
    record, retrieved = store.get_artifact("bundle-1")
    assert first.created
    assert not second.created
    assert first.record == second.record == record
    assert retrieved == payload
    assert record.size_bytes == len(payload)


def test_same_bundle_id_with_different_facts_conflicts_and_leaves_no_orphan(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    original = make_valid_bundle(bundle_id="conflict")
    conflicting = with_status(original, "failed")
    store.ingest(original, canonical(original))
    conflicting_payload = canonical(conflicting)
    conflicting_digest = __import__("hashlib").sha256(conflicting_payload).hexdigest()
    with pytest.raises(IncidentConflictError):
        store.ingest(conflicting, conflicting_payload)
    assert list(store.iter_referenced_digests()) == [store.get_record("conflict").digest]
    assert not store.objects.path_for(conflicting_digest).exists()


def test_store_survives_real_close_and_restart(tmp_path, valid_bundle) -> None:
    payload = canonical(valid_bundle)
    first = IncidentStore(tmp_path)
    first.ingest(valid_bundle, payload)
    first.close()
    restarted = IncidentStore(tmp_path)
    record, retrieved = restarted.get_artifact("bundle-1")
    assert retrieved == payload
    assert record.bundle_id == "bundle-1"


def test_external_references_are_project_scoped_catalog_data_and_survive_restart(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    result = store.ingest(valid_bundle, canonical(valid_bundle))
    first = store.set_external_reference(
        result.record.bundle_id,
        namespace="platform",
        record_type="call",
        external_id="call_123",
    )
    store.set_external_reference(
        result.record.bundle_id,
        namespace="voice_labs",
        record_type="run",
        external_id="run_456",
    )
    replaced = store.set_external_reference(
        result.record.bundle_id,
        namespace="voice_labs",
        record_type="run",
        external_id="run_789",
    )
    retried = store.set_external_reference(
        result.record.bundle_id,
        namespace="voice_labs",
        record_type="run",
        external_id="run_789",
    )

    assert first.external_id == "call_123"
    assert replaced.external_id == "run_789"
    assert retried.linked_at_unix_nano == replaced.linked_at_unix_nano
    assert [item.external_id for item in store.get_external_references("bundle-1")] == [
        "call_123",
        "run_789",
    ]
    assert store.get_record("bundle-1").digest == result.record.digest

    store.create_project("other-project", display_name="Other project")
    with pytest.raises(IncidentNotFoundError):
        store.get_external_references("bundle-1", project_id="other-project")
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incident_external_references(
                    project_id, bundle_id, namespace, record_type,
                    external_id, linked_at_unix_nano
                ) VALUES ('other-project', 'bundle-1', 'platform', 'call', 'call_123', 1)
                """
            )

    store.close()
    restarted = IncidentStore(tmp_path)
    assert [item.external_id for item in restarted.get_external_references("bundle-1")] == [
        "call_123",
        "run_789",
    ]


def test_purge_cascades_to_external_references(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.set_external_reference(
        "bundle-1",
        namespace="platform",
        record_type="call",
        external_id="call_123",
    )

    store.purge("bundle-1")

    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM incident_external_references").fetchone()[0]
            == 0
        )


def test_external_reference_write_purges_expired_incident_atomically(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.set_external_reference(
        "bundle-1",
        namespace="platform",
        record_type="call",
        external_id="call_old",
    )
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        connection.execute(
            "UPDATE incidents SET expires_at_unix_nano = ? WHERE bundle_id = ?",
            (str(time.time_ns() - 1), "bundle-1"),
        )

    with pytest.raises(IncidentPurgedError):
        store.set_external_reference(
            "bundle-1",
            namespace="platform",
            record_type="call",
            external_id="call_new",
        )

    with pytest.raises(IncidentPurgedError):
        store.get_record("bundle-1")
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM incident_external_references WHERE bundle_id = ?",
                ("bundle-1",),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tombstones WHERE project_id = ?",
                ("default",),
            ).fetchone()[0]
            == 1
        )


def test_external_reference_delete_purges_expired_incident_atomically(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.set_external_reference(
        "bundle-1",
        namespace="platform",
        record_type="call",
        external_id="call_old",
    )
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        connection.execute(
            "UPDATE incidents SET expires_at_unix_nano = ? WHERE bundle_id = ?",
            (str(time.time_ns() - 1), "bundle-1"),
        )

    with pytest.raises(IncidentPurgedError):
        store.delete_external_reference(
            "bundle-1",
            namespace="platform",
            record_type="call",
        )

    with pytest.raises(IncidentPurgedError):
        store.get_record("bundle-1")
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM incident_external_references WHERE bundle_id = ?",
                ("bundle-1",),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tombstones WHERE project_id = ?",
                ("default",),
            ).fetchone()[0]
            == 1
        )


def test_purge_physically_removes_artifact_and_leaves_nonreusable_tombstone(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    payload = canonical(valid_bundle)
    result = store.ingest(valid_bundle, payload)
    store.put_analysis("bundle-1", "1", analysis_value(store, "bundle-1", "1", "derived"))
    object_path = store.objects.path_for(result.record.digest)
    assert object_path.exists()
    store.purge("bundle-1")
    assert not object_path.exists()
    with pytest.raises(IncidentPurgedError):
        store.get_artifact("bundle-1")
    with pytest.raises(IncidentPurgedError):
        store.ingest(valid_bundle, payload)
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        assert connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0
        tombstone = connection.execute(
            "SELECT bundle_id_sha256, purged_at_unix_nano FROM tombstones"
        ).fetchone()
        assert tombstone[0] == hashlib.sha256(b"bundle-1").hexdigest()
        assert isinstance(tombstone[1], int)
    store.purge("bundle-1")  # Purge is idempotent.


def test_purge_preserves_unattributed_cas_orphans(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    orphan_digest, _ = store.objects.put(b"unattributed crash-left evidence")
    orphan_path = store.objects.path_for(orphan_digest)

    store.purge(valid_bundle.profile.manifest.bundle_id)

    assert orphan_path.read_bytes() == b"unattributed crash-left evidence"


def test_retention_maintenance_retries_failed_pending_ingest_cleanup(
    tmp_path, valid_bundle, monkeypatch, caplog
) -> None:
    store = IncidentStore(tmp_path)
    payload = canonical(valid_bundle)
    digest = hashlib.sha256(payload).hexdigest()
    object_path = store.objects.path_for(digest)
    original_put = store.objects.put
    original_delete_many = store.objects.delete_many

    def publish_then_fail(data: bytes):
        original_put(data)
        raise RuntimeError("injected failure after object publication")

    def fail_cleanup(_digests: tuple[str, ...]) -> None:
        raise OSError("injected object cleanup failure")

    monkeypatch.setattr(store.objects, "put", publish_then_fail)
    monkeypatch.setattr(store.objects, "delete_many", fail_cleanup)

    with pytest.raises(RuntimeError, match="after object publication"):
        store.ingest(valid_bundle, payload)

    assert object_path.is_file()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending_ingests").fetchone()[0] == 1
    assert "pending ingest cleanup failed; retry is deferred" in caplog.text

    monkeypatch.setattr(store.objects, "delete_many", original_delete_many)
    assert store.purge_expired(limit=1) == 0

    assert not object_path.exists()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending_ingests").fetchone()[0] == 0


def test_purge_unknown_incident_is_not_silently_tombstoned(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    with pytest.raises(IncidentNotFoundError):
        store.purge("never-seen")


def test_artifact_digest_is_verified_on_every_read(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    result = store.ingest(valid_bundle, canonical(valid_bundle))
    store.objects.path_for(result.record.digest).write_bytes(b"corrupt")
    with pytest.raises(ArtifactCorruptionError):
        store.get_artifact("bundle-1")


def test_missing_artifact_is_reported_as_corruption_not_not_found(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    result = store.ingest(valid_bundle, canonical(valid_bundle))
    store.objects.path_for(result.record.digest).unlink()
    with pytest.raises(ArtifactCorruptionError):
        store.get_artifact("bundle-1")


def test_analysis_is_versioned_separately_and_first_write_is_immutable(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    first = store.put_analysis("bundle-1", "1", analysis_value(store, "bundle-1", "1", "answer_1"))
    second = store.put_analysis(
        "bundle-1", "1", analysis_value(store, "bundle-1", "1", "answer_999")
    )
    other_version = store.put_analysis(
        "bundle-1", "2", analysis_value(store, "bundle-1", "2", "answer_2")
    )
    assert first.value["projections"]["limitations"] == ["answer_1"]
    assert second.value["projections"]["limitations"] == ["answer_1"]
    assert other_version.value["projections"]["limitations"] == ["answer_2"]
    assert first.input_digest == store.get_record("bundle-1").digest


@pytest.mark.parametrize("bad", [{"value": float("nan")}, {"value": object()}])
def test_analysis_storage_accepts_only_strict_json(tmp_path, valid_bundle, bad) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    with pytest.raises(StorageError):
        store.put_analysis("bundle-1", "bad", bad)


def test_analysis_storage_rejects_inner_digest_or_version_mismatch(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    correct = analysis_value(store, "bundle-1", "1", "ok")
    with pytest.raises(StorageError, match="input digest"):
        store.put_analysis(
            "bundle-1",
            "1",
            correct.model_copy(update={"input_sha256": "0" * 64}),
        )
    with pytest.raises(StorageError, match="version"):
        store.put_analysis("bundle-1", "2", correct)


def test_analysis_storage_preserves_full_uint64_generation_time(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    maximum = "18446744073709551615"
    analysis = analysis_value(store, "bundle-1", "max", "uint64_boundary").model_copy(
        update={"generated_at_unix_nano": maximum}
    )
    stored = store.put_analysis("bundle-1", "max", analysis)
    assert stored.generated_at_unix_nano == maximum
    assert store.get_analysis("bundle-1", "max").generated_at_unix_nano == maximum


@pytest.mark.parametrize(
    "corrupt_output",
    (
        b"{not-json",
        b'{"future":' + (b"1" * 5_000) + b"}",
        (b"[" * 2_000) + b"0" + (b"]" * 2_000),
    ),
    ids=("malformed", "oversized-integer", "recursive"),
)
def test_corrupt_analysis_json_is_detected(
    tmp_path,
    valid_bundle,
    corrupt_output: bytes,
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.put_analysis("bundle-1", "1", analysis_value(store, "bundle-1", "1", "ok"))
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        connection.execute("UPDATE analyses SET output_json = ?", (corrupt_output,))
    with pytest.raises(ArtifactCorruptionError):
        store.get_analysis("bundle-1", "1")


def test_pagination_is_stable_bounded_and_duplicate_free(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    for index in range(7):
        bundle = make_valid_bundle(bundle_id=f"bundle-{index}")
        store.ingest(bundle, canonical(bundle))
    seen: list[str] = []
    cursor = None
    while True:
        page = store.list_incidents(limit=2, cursor=cursor)
        seen.extend(item.bundle_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == 7
    assert len(set(seen)) == 7


@pytest.mark.parametrize("cursor", ["not-base64", "WzEsMl0", "e30", ""])
def test_invalid_pagination_cursor_is_rejected(tmp_path, cursor: str) -> None:
    store = IncidentStore(tmp_path)
    with pytest.raises(InvalidCursorError):
        store.list_incidents(cursor=cursor)


def test_oversized_cursor_is_rejected_before_json_parsing(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    raw = b"[" + (b" " * 600) + b"]"
    cursor = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def fail_if_parsed(_value: object) -> object:
        pytest.fail("oversized cursor reached json.loads")

    monkeypatch.setattr(storage_module.json, "loads", fail_if_parsed)
    with pytest.raises(InvalidCursorError):
        store.list_incidents(cursor=cursor)


def test_cursor_recursion_error_is_normalized(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    small_cursor = base64.urlsafe_b64encode(b'["1","bundle-1"]').rstrip(b"=").decode("ascii")

    def recurse(_value: object) -> object:
        raise RecursionError

    monkeypatch.setattr(storage_module.json, "loads", recurse)
    with pytest.raises(InvalidCursorError):
        store.list_incidents(cursor=small_cursor)


def test_oversized_cursor_timestamp_is_rejected(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    oversized_time = (
        base64.urlsafe_b64encode(('["' + ("9" * 100) + '","bundle-1"]').encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    with pytest.raises(InvalidCursorError):
        store.list_incidents(cursor=oversized_time)


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_storage_limit_is_bounded(tmp_path, limit: int) -> None:
    with pytest.raises(ValueError):
        IncidentStore(tmp_path).list_incidents(limit=limit)


def test_session_filter_is_parameterized_against_sql_metacharacters(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    injection = "session' OR 1=1 --"
    special = make_valid_bundle(bundle_id="special")
    manifest = special.profile.manifest.model_copy(update={"session_id": injection})
    session = special.profile.session.model_copy(update={"session_id": injection})
    participants = tuple(
        item.model_copy(update={"session_id": injection}) for item in special.profile.participants
    )
    streams = tuple(
        item.model_copy(update={"session_id": injection}) for item in special.profile.audio_streams
    )
    operations = tuple(
        item.model_copy(update={"session_id": injection}) for item in special.profile.operations
    )
    events = tuple(
        item.model_copy(update={"session_id": injection}) for item in special.profile.events
    )
    profile = special.profile.model_copy(
        update={
            "manifest": manifest,
            "session": session,
            "participants": participants,
            "audio_streams": streams,
            "operations": operations,
            "events": events,
        }
    )
    special = special.model_copy(update={"profile": profile})
    normal = make_valid_bundle(bundle_id="normal")
    store.ingest(special, canonical(special))
    store.ingest(normal, canonical(normal))
    page = store.list_incidents(session_id=injection)
    assert [item.bundle_id for item in page.items] == ["special"]


def test_concurrent_identical_ingest_creates_one_record(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    payload = canonical(valid_bundle)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.ingest(valid_bundle, payload), range(40)))
    assert sum(result.created for result in results) == 1
    assert len({result.record.digest for result in results}) == 1
    assert len(store.list_incidents().items) == 1


def test_concurrent_conflicting_ingest_has_one_winner_and_no_partial_rows(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    completed = make_valid_bundle(bundle_id="race")
    failed = with_status(completed, "failed")
    candidates = [(completed, canonical(completed)), (failed, canonical(failed))] * 10

    def ingest(candidate):
        try:
            return store.ingest(*candidate).record.digest
        except IncidentConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(ingest, candidates))
    winners = {item for item in results if item != "conflict"}
    assert len(winners) == 1
    assert len(store.list_incidents().items) == 1
    assert results.count("conflict") >= 1


def test_named_shared_memory_database_works_across_operation_connections(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path, database_path=":memory:")
    store.ingest(valid_bundle, canonical(valid_bundle))
    assert store.get_record("bundle-1").session_id == "session-1"
    assert store.ready()


def test_cas_publication_is_locked_through_indexing_against_cleanup(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    writer = IncidentStore(tmp_path)
    cleaner = IncidentStore(tmp_path)
    payload = canonical(valid_bundle)
    published = threading.Event()
    release = threading.Event()
    original_put = writer.objects.put

    def paused_put(value: bytes):
        result = original_put(value)
        published.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(writer.objects, "put", paused_put)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest_future = pool.submit(writer.ingest, valid_bundle, payload)
        assert published.wait(2)
        cleanup_future = pool.submit(cleaner.cleanup_unreferenced_objects)
        try:
            time.sleep(0.05)
            assert not cleanup_future.done()
        finally:
            release.set()
        assert ingest_future.result(timeout=3).created
        assert cleanup_future.result(timeout=3) == 0

    assert cleaner.get_artifact("bundle-1")[1] == payload


@pytest.mark.skipif(
    not hasattr(os, "fork") or storage_module.fcntl is None,
    reason="requires POSIX fork and advisory file locks",
)
def test_forked_store_reopens_its_advisory_lock(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    read_fd, write_fd = os.pipe()
    child_pid = -1
    entered_while_parent_held_lock = False
    try:
        with store._mutation():
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    with store._mutation():
                        os.write(write_fd, b"entered")
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(write_fd)
            entered_while_parent_held_lock = bool(select.select([read_fd], [], [], 0.1)[0])

        ready = select.select([read_fd], [], [], 3)[0]
        assert ready, "forked storage process never acquired the released mutation lock"
        assert os.read(read_fd, 16) == b"entered"
        _, wait_status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(wait_status) == 0
    finally:
        os.close(read_fd)
        if child_pid > 0:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(child_pid, os.WNOHANG)
        store.close()

    assert not entered_while_parent_held_lock, (
        "forked process reused the parent's open-file-description lock"
    )


@pytest.mark.skipif(
    not hasattr(os, "fork") or storage_module.fcntl is None,
    reason="requires POSIX fork and advisory file locks",
)
def test_forked_in_memory_store_reports_not_ready(tmp_path) -> None:
    store = IncidentStore(tmp_path, database_path=":memory:")
    read_fd, write_fd = os.pipe()
    child_pid = -1
    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                is_ready = store.ready()
                try:
                    with store._mutation():
                        mutation_rejected = False
                except StorageError:
                    mutation_rejected = True
                os.write(write_fd, bytes((is_ready, mutation_rejected)))
                os._exit(0)
            except BaseException:
                os._exit(1)

        os.close(write_fd)
        assert select.select([read_fd], [], [], 3)[0]
        result = os.read(read_fd, 2)
        _, wait_status = os.waitpid(child_pid, 0)
    finally:
        os.close(read_fd)
        store.close()

    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert result == bytes((False, True))


def test_ingest_populates_and_restart_reconciles_graph_projection(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    expected_links = sum(len(operation.links) for operation in valid_bundle.profile.operations)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == len(
            valid_bundle.profile.operations
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM causal_links").fetchone()[0] == expected_links
        )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(
            valid_bundle.profile.events
        )
        row = connection.execute(
            """
            SELECT operation_name, trace_id, span_id, started_clock_domain_id
            FROM operations WHERE bundle_id = ? AND operation_id = ?
            """,
            ("bundle-1", valid_bundle.profile.operations[0].operation_id),
        ).fetchone()
        assert row == (
            valid_bundle.profile.operations[0].operation_name,
            valid_bundle.profile.operations[0].trace_id,
            valid_bundle.profile.operations[0].span_id,
            valid_bundle.profile.operations[0].started_at.clock_domain_id,
        )
        connection.execute("DELETE FROM events")
        connection.execute("DELETE FROM causal_links")
        connection.execute("DELETE FROM operations")
    store.close()

    restarted = IncidentStore(tmp_path)
    with sqlite3.connect(restarted.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == len(
            valid_bundle.profile.operations
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM causal_links").fetchone()[0] == expected_links
        )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(
            valid_bundle.profile.events
        )


def test_reconciliation_rolls_back_partial_repairs_and_retries_after_cas_restore(
    tmp_path,
) -> None:
    repairable = make_valid_bundle(bundle_id="a-repairable")
    temporarily_corrupt = make_valid_bundle(bundle_id="z-temporarily-corrupt")
    repairable_payload = canonical(repairable)
    corrupt_payload = canonical(temporarily_corrupt)
    store = IncidentStore(tmp_path)
    store.ingest(repairable, repairable_payload)
    corrupt_record = store.ingest(temporarily_corrupt, corrupt_payload).record
    corrupt_path = store.objects.path_for(corrupt_record.digest)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM events WHERE bundle_id = 'a-repairable'")
        connection.execute("DELETE FROM causal_links WHERE bundle_id = 'a-repairable'")
        connection.execute("DELETE FROM operations WHERE bundle_id = 'a-repairable'")
        connection.execute("DELETE FROM turn_metrics WHERE bundle_id = 'a-repairable'")
    store.close()
    corrupt_path.write_bytes(b"crash-left partial object")

    with pytest.raises(ArtifactCorruptionError, match="digest mismatch"):
        IncidentStore(tmp_path)

    with sqlite3.connect(tmp_path / "earshot.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operations WHERE bundle_id = 'a-repairable'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM turn_metrics WHERE bundle_id = 'a-repairable'"
            ).fetchone()[0]
            == 0
        )

    corrupt_path.write_bytes(corrupt_payload)
    restarted = IncidentStore(tmp_path)

    assert restarted.get_artifact("a-repairable")[1] == repairable_payload
    assert restarted.get_artifact("z-temporarily-corrupt")[1] == corrupt_payload
    assert {fact.bundle_id for fact in restarted.list_turn_facts()} == {
        "a-repairable",
        "z-temporarily-corrupt",
    }


def test_retention_deadline_is_indexed_and_expired_incidents_are_purged(
    tmp_path, valid_bundle
) -> None:
    created = int(valid_bundle.profile.manifest.created_at_unix_nano)
    policies = tuple(
        policy.model_copy(update={"retention": RetentionPolicy(ttl_nano="10")})
        if policy.capture_class == "metadata"
        else policy
        for policy in valid_bundle.profile.privacy.capture_classes
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    bundle = valid_bundle.model_copy(
        update={"profile": valid_bundle.profile.model_copy(update={"privacy": privacy})}
    )
    store = IncidentStore(tmp_path)
    store.ingest(bundle, canonical(bundle))
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT expires_at_unix_nano FROM incidents WHERE bundle_id = 'bundle-1'"
        ).fetchone()[0] == str(created + 10)
    assert store.purge_expired(created + 9) == 0
    assert store.purge_expired(str(created + 10)) == 1
    with pytest.raises(IncidentPurgedError):
        store.get_record("bundle-1")


def test_expiry_batch_removes_each_unreferenced_artifact(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundles = tuple(
        with_metadata_retention(
            make_valid_bundle(bundle_id=f"expired-batch-{index}"),
            RetentionPolicy(expires_at_unix_nano="0"),
        )
        for index in range(3)
    )
    digests = tuple(store.ingest(bundle, canonical(bundle)).record.digest for bundle in bundles)
    object_paths = tuple(store.objects.path_for(digest) for digest in digests)
    assert all(path.is_file() for path in object_paths)

    assert store.purge_expired(limit=len(bundles)) == len(bundles)

    assert all(not path.exists() for path in object_paths)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == len(bundles)
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_object_deletions").fetchone()[0] == 0
        )


def test_batched_expiry_cleanup_securely_scrubs_large_backlogs(tmp_path) -> None:
    secret = "expiry_batch_scrub_marker_6d06ac0d"
    bundle = make_valid_bundle(bundle_id="expired-multibatch-secret")
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    store.put_analysis(
        bundle.profile.manifest.bundle_id,
        "expiry-batch-test",
        analysis_value(
            store,
            bundle.profile.manifest.bundle_id,
            "expiry-batch-test",
            secret,
        ),
    )
    synthetic_count = 10_000
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE incidents SET expires_at_unix_nano = '0' WHERE bundle_id = ?",
            (bundle.profile.manifest.bundle_id,),
        )
        connection.executemany(
            """
            INSERT INTO incidents(
                bundle_id, project_id, session_id, schema_version, object_digest,
                size_bytes, status, finality, completeness, framework,
                created_at_unix_nano, ingested_at_unix_nano, expires_at_unix_nano,
                export_allowed_local_api, export_allowed_local_cli
            ) VALUES (?, 'default', 'synthetic-session', '1', ?, 1,
                      'complete', 'final', 'complete', NULL, '1', ?, '0', 1, 1)
            """,
            (
                (f"expired-multibatch-{index:05d}", result.record.digest, index)
                for index in range(synthetic_count)
            ),
        )

    assert store.list_incidents().items == ()
    assert store.purge_expired(limit=10_000, compact_when_drained=True) == synthetic_count
    assert store.objects.path_for(result.record.digest).is_file()
    assert store.purge_expired(limit=10_000, compact_when_drained=True) == 1
    assert not store.objects.path_for(result.record.digest).exists()
    assert all(
        secret.encode() not in path.read_bytes()
        for path in tmp_path.glob("earshot.sqlite3*")
        if path.is_file()
    )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == (
            synthetic_count + 1
        )


def test_project_incident_list_hides_only_that_projects_expired_records(tmp_path) -> None:
    retention = RetentionPolicy(expires_at_unix_nano="0")
    first = with_metadata_retention(make_valid_bundle(bundle_id="expired-first"), retention)
    second = with_metadata_retention(make_valid_bundle(bundle_id="expired-second"), retention)
    store = IncidentStore(tmp_path)
    store.create_project("first-project", display_name="First")
    store.create_project("second-project", display_name="Second")
    store.ingest(first, project_id="first-project")
    store.ingest(second, project_id="second-project")

    assert store.list_incidents(project_id="first-project").items == ()

    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        remaining = connection.execute("SELECT project_id, bundle_id FROM incidents").fetchall()
    assert {(row[0], row[1]) for row in remaining} == {
        ("first-project", "expired-first"),
        ("second-project", "expired-second"),
    }


def test_expired_incident_is_physically_purged_at_direct_read_boundary(tmp_path) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-read"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    object_path = store.objects.path_for(result.record.digest)

    with pytest.raises(IncidentPurgedError):
        store.get_artifact("expired-read")
    assert not object_path.exists()


def test_expired_direct_read_defers_database_compaction_to_maintenance(
    tmp_path, monkeypatch
) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-direct-read-compaction"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    store.ingest(bundle, canonical(bundle))
    original_scrub = store._scrub_deleted_pages
    scrub_calls = 0

    def count_scrubs(*, compact=True, skip_if_current=False):
        nonlocal scrub_calls
        scrub_calls += 1
        return original_scrub(compact=compact, skip_if_current=skip_if_current)

    monkeypatch.setattr(store, "_scrub_deleted_pages", count_scrubs)
    with pytest.raises(IncidentPurgedError):
        store.get_record("expired-direct-read-compaction")

    assert scrub_calls == 0
    assert store.list_incidents().items == ()
    assert store.purge_expired() == 0
    assert scrub_calls == 1


def test_explicit_purge_compacts_after_releasing_the_store_lock(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    original_scrub = store._scrub_deleted_pages
    compaction_started = threading.Event()
    release_compaction = threading.Event()

    def pause_compaction(*, compact=True, skip_if_current=False):
        if compact:
            compaction_started.set()
            assert release_compaction.wait(3)
        return original_scrub(compact=compact, skip_if_current=skip_if_current)

    monkeypatch.setattr(store, "_scrub_deleted_pages", pause_compaction)
    with ThreadPoolExecutor(max_workers=2) as executor:
        purge = executor.submit(store.purge, "bundle-1")
        assert compaction_started.wait(2)
        listing = executor.submit(store.list_incidents)
        try:
            page = listing.result(timeout=0.5)
        finally:
            release_compaction.set()
        purge.result(timeout=3)

    assert page.items == ()


def test_successful_purge_retry_and_restart_do_not_revacuum(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    original_scrub = IncidentStore._scrub_deleted_pages
    scrub_calls = 0

    def count_scrubs(store, *, compact=True, skip_if_current=False):
        nonlocal scrub_calls
        scrub_calls += 1
        return original_scrub(
            store,
            compact=compact,
            skip_if_current=skip_if_current,
        )

    monkeypatch.setattr(IncidentStore, "_scrub_deleted_pages", count_scrubs)
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.purge("bundle-1")
    assert scrub_calls == 1

    store.purge("bundle-1")
    assert scrub_calls == 1
    store.close()

    restarted = IncidentStore(tmp_path)
    assert scrub_calls == 1
    restarted.close()


def test_startup_compaction_does_not_hold_the_store_lock(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE storage_maintenance SET purge_generation = purge_generation + 1 "
            "WHERE singleton = 1"
        )

    original_scrub = IncidentStore._scrub_deleted_pages
    compaction_started = threading.Event()
    release_compaction = threading.Event()

    def pause_compaction(instance, *, compact=True, skip_if_current=False):
        if instance is not store:
            compaction_started.set()
            assert release_compaction.wait(3)
        return original_scrub(
            instance,
            compact=compact,
            skip_if_current=skip_if_current,
        )

    monkeypatch.setattr(IncidentStore, "_scrub_deleted_pages", pause_compaction)
    with ThreadPoolExecutor(max_workers=2) as executor:
        startup = executor.submit(IncidentStore, tmp_path)
        assert compaction_started.wait(2)
        listing = executor.submit(store.list_incidents)
        try:
            page = listing.result(timeout=0.5)
        finally:
            release_compaction.set()
        restarted = startup.result(timeout=3)

    assert len(page.items) == 1
    restarted.close()
    store.close()


def test_concurrent_compactions_share_a_lock_and_recheck_the_scrub_generation(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    first_store = IncidentStore(tmp_path)
    second_store = IncidentStore(tmp_path)
    first_store.ingest(valid_bundle, canonical(valid_bundle))
    with sqlite3.connect(first_store.database_path) as connection:
        connection.execute(
            "UPDATE storage_maintenance SET purge_generation = purge_generation + 1 "
            "WHERE singleton = 1"
        )

    original_scrub = getattr(IncidentStore, "_scrub_deleted_pages_locked", None)
    first_scrub_started = threading.Event()
    second_scrub_started = threading.Event()
    release_first_scrub = threading.Event()

    def pause_scrub(store, *args, **kwargs):
        if store is first_store:
            first_scrub_started.set()
            assert release_first_scrub.wait(3)
        else:
            second_scrub_started.set()
        if original_scrub is not None:
            return original_scrub(store, *args, **kwargs)
        return None

    monkeypatch.setattr(IncidentStore, "_scrub_deleted_pages_locked", pause_scrub, raising=False)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(first_store._scrub_deleted_pages, skip_if_current=True)
            assert first_scrub_started.wait(2)
            second = executor.submit(second_store._scrub_deleted_pages, skip_if_current=True)
            assert not second_scrub_started.wait(0.1)
            release_first_scrub.set()
            first.result(timeout=3)
            second.result(timeout=3)
        assert second_scrub_started.is_set()
    finally:
        release_first_scrub.set()
        first_store.close()
        second_store.close()


@pytest.mark.skipif(
    not hasattr(os, "fork") or storage_module.fcntl is None,
    reason="requires POSIX fork and advisory file locks",
)
def test_forked_compaction_reopens_its_advisory_lock(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE storage_maintenance SET purge_generation = purge_generation + 1 "
            "WHERE singleton = 1"
        )

    read_fd, write_fd = os.pipe()
    child_pid = -1
    entered_while_parent_held_lock = False
    try:
        with store._compaction_thread_lock:
            storage_module.fcntl.flock(
                store._compaction_lock_handle.fileno(), storage_module.fcntl.LOCK_EX
            )
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    store._scrub_deleted_pages(skip_if_current=True)
                    os.write(write_fd, b"done")
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(write_fd)
            entered_while_parent_held_lock = bool(select.select([read_fd], [], [], 0.1)[0])
            storage_module.fcntl.flock(
                store._compaction_lock_handle.fileno(), storage_module.fcntl.LOCK_UN
            )

        assert select.select([read_fd], [], [], 3)[0]
        assert os.read(read_fd, 8) == b"done"
        _, wait_status = os.waitpid(child_pid, 0)
    finally:
        os.close(read_fd)
        store.close()

    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert not entered_while_parent_held_lock, (
        "forked process reused the parent's compaction file lock"
    )


def test_scrub_racing_store_close_fails_with_storage_error(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    original_ensure = store._ensure_process_ownership
    ownership_checked = threading.Event()
    resume_scrub = threading.Event()

    def pause_after_ownership_check():
        original_ensure()
        ownership_checked.set()
        assert resume_scrub.wait(3)

    monkeypatch.setattr(store, "_ensure_process_ownership", pause_after_ownership_check)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            scrub = executor.submit(store._scrub_deleted_pages, skip_if_current=True)
            assert ownership_checked.wait(2)
            store.close()
            resume_scrub.set()
            with pytest.raises(StorageError, match="closed"):
                scrub.result(timeout=2)
    finally:
        resume_scrub.set()
        store.close()


def test_list_hides_expired_incidents_until_the_expiry_reaper_runs(tmp_path) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-list"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    object_path = store.objects.path_for(result.record.digest)

    assert store.list_incidents().items == ()
    assert object_path.is_file()
    with sqlite3.connect(store.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE bundle_id = 'expired-list'"
            ).fetchone()[0]
            == 1
        )

    assert store.purge_expired() == 1
    assert not object_path.exists()
    with pytest.raises(IncidentPurgedError):
        store.get_record("expired-list")


def test_startup_purges_incidents_that_expired_while_store_was_offline(tmp_path) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-startup"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    object_path = store.objects.path_for(result.record.digest)
    store.close()

    restarted = IncidentStore(tmp_path)
    assert not object_path.exists()
    with pytest.raises(IncidentPurgedError):
        restarted.get_record("expired-startup")


def test_startup_expiry_cleanup_releases_store_lock_between_batches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage_module, "_STARTUP_EXPIRED_PURGE_BATCH_SIZE", 1)
    store = IncidentStore(tmp_path)
    for bundle_id in ("expired-startup-batch-a", "expired-startup-batch-b"):
        bundle = with_metadata_retention(
            make_valid_bundle(bundle_id=bundle_id),
            RetentionPolicy(expires_at_unix_nano="0"),
        )
        store.ingest(bundle, canonical(bundle))

    original_purge_batch = IncidentStore._purge_expired_reconciliation_batch
    first_batch_finished = threading.Event()
    release_startup = threading.Event()
    startup_batches = 0

    def one_at_a_time(instance, now, bundle_ids, *, ingest_sequence_high_water):
        nonlocal startup_batches
        removed = original_purge_batch(
            instance,
            now,
            bundle_ids[:1],
            ingest_sequence_high_water=ingest_sequence_high_water,
        )
        if instance is not store and removed:
            startup_batches += 1
            if startup_batches == 1:
                first_batch_finished.set()
                assert release_startup.wait(3)
        return removed

    monkeypatch.setattr(IncidentStore, "_purge_expired_reconciliation_batch", one_at_a_time)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            startup = executor.submit(IncidentStore, tmp_path)
            assert first_batch_finished.wait(2)
            listing = executor.submit(store.list_incidents)
            assert listing.result(timeout=0.5).items == ()
            release_startup.set()
            restarted = startup.result(timeout=5)
        assert startup_batches == 2
        restarted.close()
    finally:
        release_startup.set()
        store.close()


def test_startup_expiry_selection_releases_store_lock_during_index_scan(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    expired = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-selection-lock"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store.ingest(expired, canonical(expired))
    original_select = IncidentStore._expired_bundle_ids
    selection_finished = threading.Event()
    release_selection = threading.Event()

    def pause_after_selection(
        instance,
        now,
        limit,
        *,
        project_id=None,
        ingest_sequence_high_water=None,
    ):
        bundle_ids = original_select(
            instance,
            now,
            limit,
            project_id=project_id,
            ingest_sequence_high_water=ingest_sequence_high_water,
        )
        if instance is not store and not selection_finished.is_set():
            selection_finished.set()
            assert release_selection.wait(3)
        return bundle_ids

    monkeypatch.setattr(IncidentStore, "_expired_bundle_ids", pause_after_selection)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            startup = executor.submit(IncidentStore, tmp_path)
            assert selection_finished.wait(2)
            listing = executor.submit(store.list_incidents)
            assert listing.result(timeout=0.5).items == ()
            release_selection.set()
            restarted = startup.result(timeout=5)
        restarted.close()
    finally:
        release_selection.set()
        store.close()


def test_startup_expiry_sweep_excludes_earlier_arrivals_from_initial_backlog(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(storage_module, "_STARTUP_EXPIRED_PURGE_BATCH_SIZE", 1)
    store = IncidentStore(tmp_path)
    original = IncidentStore._purge_expired_reconciliation_batch
    initial_ids: list[str] = []
    initial_objects: list[Path] = []
    for index in range(3):
        bundle_id = f"expired-sweep-initial-{index}"
        initial = with_metadata_retention(
            make_valid_bundle(bundle_id=bundle_id),
            RetentionPolicy(expires_at_unix_nano="100"),
        )
        result = store.ingest(initial, canonical(initial))
        initial_ids.append(bundle_id)
        initial_objects.append(store.objects.path_for(result.record.digest))
    arrivals: list[str] = []
    arrival_objects: list[Path] = []

    def replenish_after_each_batch(instance, now, bundle_ids, *, ingest_sequence_high_water):
        if instance is store and len(arrivals) < 3:
            bundle_id = f"expired-sweep-arrival-{len(arrivals)}"
            arrival = with_metadata_retention(
                make_valid_bundle(bundle_id=bundle_id),
                RetentionPolicy(expires_at_unix_nano="0"),
            )
            result = store.ingest(arrival, canonical(arrival))
            arrivals.append(bundle_id)
            arrival_objects.append(store.objects.path_for(result.record.digest))
        removed = original(
            instance,
            now,
            bundle_ids,
            ingest_sequence_high_water=ingest_sequence_high_water,
        )
        return removed

    monkeypatch.setattr(
        IncidentStore,
        "_purge_expired_reconciliation_batch",
        replenish_after_each_batch,
    )
    try:
        removed = store._purge_expired_for_reconciliation(str(time.time_ns()))
        assert removed == 3
        assert arrivals == [
            "expired-sweep-arrival-0",
            "expired-sweep-arrival-1",
            "expired-sweep-arrival-2",
        ]
        with sqlite3.connect(store.database_path) as connection:
            stored_ids = {row[0] for row in connection.execute("SELECT bundle_id FROM incidents")}
        assert stored_ids == set(arrivals)
        assert set(initial_ids).isdisjoint(stored_ids)
        assert all(not path.exists() for path in initial_objects)
        assert all(path.exists() for path in arrival_objects)
    finally:
        store.close()


def test_startup_skips_expired_artifact_arriving_during_reconciliation_decode(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    original_sweep = IncidentStore._purge_expired_for_reconciliation
    injected = False
    expired_object_path: Path | None = None

    def inject_expired_corrupt_artifact(instance, now):
        nonlocal injected, expired_object_path
        removed = original_sweep(instance, now)
        if instance is not store and not injected:
            injected = True
            bundle = with_metadata_retention(
                make_valid_bundle(bundle_id="expired-arrived-during-startup"),
                RetentionPolicy(expires_at_unix_nano="0"),
            )
            result = store.ingest(bundle, canonical(bundle))
            expired_object_path = store.objects.path_for(result.record.digest)
            expired_object_path.write_bytes(b"corrupt but already expired")
        return removed

    monkeypatch.setattr(
        IncidentStore, "_purge_expired_for_reconciliation", inject_expired_corrupt_artifact
    )
    try:
        restarted = IncidentStore(tmp_path)
        assert injected
        assert expired_object_path is not None
        assert not expired_object_path.exists()
        with sqlite3.connect(restarted.database_path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE bundle_id = 'expired-arrived-during-startup'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM tombstones WHERE bundle_id_sha256 = ?",
                    (hashlib.sha256(b"expired-arrived-during-startup").hexdigest(),),
                ).fetchone()[0]
                == 1
            )
        with pytest.raises(IncidentPurgedError):
            restarted.get_record("expired-arrived-during-startup")
        restarted.close()
    finally:
        store.close()


def test_startup_retention_purges_expired_artifact_even_if_bytes_are_corrupt(
    tmp_path,
) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-corrupt-startup"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    object_path = store.objects.path_for(result.record.digest)
    object_path.write_bytes(b"corrupt but already expired")
    store.close()

    restarted = IncidentStore(tmp_path)
    assert not object_path.exists()
    with pytest.raises(IncidentPurgedError):
        restarted.get_record("expired-corrupt-startup")


def test_destination_filtered_listing_never_pages_through_restricted_ids(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    restricted = make_valid_bundle(bundle_id="restricted")
    policies = tuple(
        policy.model_copy(update={"export": ExportPolicy(allowed=False)})
        if policy.capture_class == "metadata"
        else policy
        for policy in restricted.profile.privacy.capture_classes
    )
    restricted = restricted.model_copy(
        update={
            "profile": restricted.profile.model_copy(
                update={
                    "privacy": restricted.profile.privacy.model_copy(
                        update={"capture_classes": policies}
                    )
                }
            )
        }
    )
    allowed = make_valid_bundle(bundle_id="allowed")
    store.ingest(restricted, canonical(restricted))
    store.ingest(allowed, canonical(allowed))

    assert {item.bundle_id for item in store.list_incidents().items} == {
        "allowed",
        "restricted",
    }
    for destination in ("local_api", "local_cli"):
        page = store.list_incidents(destination=destination, limit=1)
        assert [item.bundle_id for item in page.items] == ["allowed"]
        assert page.next_cursor is None
    with pytest.raises(ValueError):
        store.list_incidents(destination="attacker")


def test_secure_purge_scrubs_analysis_secret_from_sqlite_files(tmp_path, valid_bundle) -> None:
    secret = b"purge_forensic_marker_835f0d6c"
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.put_analysis(
        "bundle-1",
        "secret",
        analysis_value(store, "bundle-1", "secret", secret.decode()),
    )
    assert any(
        secret in path.read_bytes() for path in tmp_path.glob("earshot.sqlite3*") if path.is_file()
    )

    store.purge("bundle-1")
    for path in tmp_path.glob("earshot.sqlite3*"):
        if path.is_file():
            assert secret not in path.read_bytes(), path


def test_startup_preserves_orphans_until_explicit_cleanup_and_cleans_temp(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    digest, _ = store.objects.put(b"unindexed crash leftover")
    temporary = store.objects.tmp / "object-crash-leftover"
    temporary.write_bytes(b"partial")
    store.close()

    restarted = IncidentStore(tmp_path)
    assert restarted.objects.path_for(digest).exists()
    assert not temporary.exists()
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert Path(restarted.database_path).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".store.lock").stat().st_mode & 0o777 == 0o600
    assert restarted.objects.root.stat().st_mode & 0o777 == 0o700
    assert restarted.objects.tmp.stat().st_mode & 0o777 == 0o700
    assert restarted.cleanup_unreferenced_objects() == 1
    assert not restarted.objects.path_for(digest).exists()


def test_missing_sqlite_catalog_fails_closed_without_deleting_cas(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    record = store.ingest(valid_bundle, canonical(valid_bundle)).record
    object_path = store.objects.path_for(record.digest)
    database_path = Path(store.database_path)
    store.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)

    with pytest.raises(StorageError, match="restore the SQLite catalog"):
        IncidentStore(tmp_path)

    assert object_path.exists()
    assert object_path.read_bytes() == canonical(valid_bundle)


def test_v1_empty_database_is_migrated_to_current_schema(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE incidents (
                bundle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                object_digest TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                finality TEXT NOT NULL,
                completeness TEXT NOT NULL,
                framework TEXT,
                created_at_unix_nano TEXT NOT NULL,
                ingested_at_unix_nano INTEGER NOT NULL
            );
            CREATE INDEX incidents_session_idx
                ON incidents(session_id, ingested_at_unix_nano DESC, bundle_id DESC);
            """
        )
    store = IncidentStore(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        columns = {row[1] for row in connection.execute("PRAGMA table_info(incidents)")}
        assert {
            "expires_at_unix_nano",
            "export_allowed_local_api",
            "export_allowed_local_cli",
        } <= columns
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
                "('operations', 'causal_links', 'events')"
            ).fetchone()[0]
            == 3
        )
        session_index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='incidents_session_idx'"
        ).fetchone()[0]
        assert "project_id" in session_index_sql


def test_v2_integer_analysis_time_is_atomically_migrated_to_text(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    original = store.put_analysis(
        "bundle-1",
        "v2-analysis",
        analysis_value(store, "bundle-1", "v2-analysis", "migration_marker"),
    )
    store.close()

    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            ALTER TABLE analyses RENAME TO analyses_text_time;
            CREATE TABLE analyses (
                bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
                analyzer_version TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                generated_at_unix_nano INTEGER NOT NULL,
                output_json BLOB NOT NULL,
                PRIMARY KEY (bundle_id, analyzer_version, input_digest)
            );
            INSERT INTO analyses
            SELECT bundle_id, analyzer_version, input_digest,
                   CAST(generated_at_unix_nano AS INTEGER), output_json
            FROM analyses_text_time;
            DROP TABLE analyses_text_time;
            PRAGMA user_version = 2;
            """
        )

    migrated = IncidentStore(tmp_path)
    restored = migrated.get_analysis("bundle-1", "v2-analysis")
    assert restored is not None
    assert restored.value == original.value
    assert restored.generated_at_unix_nano == original.generated_at_unix_nano
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        column = next(
            row
            for row in connection.execute("PRAGMA table_info(analyses)")
            if row[1] == "generated_at_unix_nano"
        )
        assert column[2].upper() == "TEXT"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v14_database_migrates_scrub_state_and_listing_indexes(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE storage_maintenance")
        for column in ("api", "cli"):
            connection.execute(f"DROP INDEX incidents_export_local_{column}_idx")
            connection.execute(
                f"CREATE INDEX incidents_export_local_{column}_idx "
                f"ON incidents(export_allowed_local_{column}, "
                "ingested_at_unix_nano DESC, bundle_id DESC)"
            )
        connection.execute("PRAGMA user_version = 14")

    migrated = IncidentStore(tmp_path)

    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert connection.execute(
            "SELECT purge_generation, scrubbed_generation FROM storage_maintenance "
            "WHERE singleton = 1"
        ).fetchone() == (0, 0)
        for column in ("api", "cli"):
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                f"AND name = 'incidents_export_local_{column}_idx'"
            ).fetchone()[0]
            assert "project_id" in index_sql


def test_v16_migration_backfills_monotonic_incident_ingest_order(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TRIGGER incidents_track_ingest_order;
            DROP TABLE incident_ingest_order;
            PRAGMA user_version = 16;
            """
        )

    migrated = IncidentStore(tmp_path)
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        old_sequence = connection.execute(
            "SELECT sequence FROM incident_ingest_order WHERE bundle_id = ?",
            (valid_bundle.profile.manifest.bundle_id,),
        ).fetchone()[0]
        assert old_sequence > 0

    next_bundle = make_valid_bundle(bundle_id="after-v16-migration")
    migrated.ingest(next_bundle, canonical(next_bundle))
    with sqlite3.connect(migrated.database_path) as connection:
        new_sequence = connection.execute(
            "SELECT sequence FROM incident_ingest_order WHERE bundle_id = ?",
            ("after-v16-migration",),
        ).fetchone()[0]
        assert new_sequence > old_sequence
    migrated.close()


def test_legacy_tombstones_seed_one_scrub_generation_on_migration(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tombstones(bundle_id_sha256, project_id, purged_at_unix_nano) "
            "VALUES (?, 'default', 1)",
            ("a" * 64,),
        )
        connection.execute("PRAGMA user_version = 14")

    original_scrub = IncidentStore._scrub_deleted_pages
    scrub_calls = 0

    def count_scrubs(instance, *, compact=True, skip_if_current=False):
        nonlocal scrub_calls
        scrub_calls += 1
        return original_scrub(
            instance,
            compact=compact,
            skip_if_current=skip_if_current,
        )

    monkeypatch.setattr(IncidentStore, "_scrub_deleted_pages", count_scrubs)
    migrated = IncidentStore(tmp_path)

    assert scrub_calls == 1
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute(
            "SELECT purge_generation, scrubbed_generation FROM storage_maintenance "
            "WHERE singleton = 1"
        ).fetchone() == (1, 1)
    migrated.close()


def test_v3_plaintext_tombstone_id_is_migrated_to_a_digest(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    sensitive_id = "alice.example.com"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tombstones (
                bundle_id TEXT PRIMARY KEY,
                purged_at_unix_nano INTEGER NOT NULL
            );
            PRAGMA user_version = 3;
            """
        )
        connection.execute(
            "INSERT INTO tombstones(bundle_id, purged_at_unix_nano) VALUES (?, ?)",
            (sensitive_id, 123),
        )

    migrated = IncidentStore(tmp_path)
    with sqlite3.connect(migrated.database_path) as connection:
        row = connection.execute(
            "SELECT bundle_id_sha256, project_id, purged_at_unix_nano FROM tombstones"
        ).fetchone()
        assert row == (
            hashlib.sha256(sensitive_id.encode()).hexdigest(),
            "default",
            123,
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18


def test_rolled_back_schema_version_does_not_exempt_hashed_tombstones_from_key_recovery(
    tmp_path,
) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tombstones(bundle_id_sha256, project_id, purged_at_unix_nano) "
            "VALUES (?, 'default', 1)",
            (hashlib.sha256(b"already-hashed-tombstone").hexdigest(),),
        )
        # A restored or manually edited version integer must not make modern
        # digest rows look like the legacy plaintext layout.
        connection.execute("PRAGMA user_version = 3")
    (tmp_path / "instance-correlation.key").unlink()

    with pytest.raises(StorageError, match="missing its correlation key"):
        IncidentStore(tmp_path)


def test_released_v4_catalog_is_migrated_without_losing_canonical_evidence(
    tmp_path, valid_bundle
) -> None:
    payload = canonical(valid_bundle)
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, payload)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP INDEX incidents_session_idx;
            DROP INDEX incidents_project_idx;
            DROP INDEX incidents_expiry_idx;
            DROP INDEX incidents_export_local_api_idx;
            DROP INDEX incidents_export_local_cli_idx;
            DROP INDEX incidents_project_bundle_idx;
            DROP TABLE external_identities;
            DROP TABLE delivery_receipts;
            DROP TABLE connectors;
            DROP TABLE api_keys;
            DROP TABLE turn_metrics;
            ALTER TABLE tombstones RENAME TO tombstones_scoped;
            CREATE TABLE tombstones (
                bundle_id_sha256 TEXT PRIMARY KEY CHECK (length(bundle_id_sha256) = 64),
                purged_at_unix_nano INTEGER NOT NULL
            );
            INSERT INTO tombstones(bundle_id_sha256, purged_at_unix_nano)
            SELECT bundle_id_sha256, purged_at_unix_nano FROM tombstones_scoped;
            DROP TABLE tombstones_scoped;
            ALTER TABLE incidents DROP COLUMN project_id;
            ALTER TABLE incidents DROP COLUMN expires_at_unix_nano;
            ALTER TABLE incidents DROP COLUMN export_allowed_local_api;
            ALTER TABLE incidents DROP COLUMN export_allowed_local_cli;
            DROP TABLE projects;
            CREATE INDEX incidents_session_idx
                ON incidents(session_id, ingested_at_unix_nano DESC, bundle_id DESC);
            PRAGMA user_version = 4;
            """
        )

    migrated = IncidentStore(tmp_path)

    record, restored = migrated.get_artifact("bundle-1")
    assert restored == payload
    assert record.session_id == valid_bundle.profile.manifest.session_id
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_current_v12_catalog_reopens_without_rewriting_authoritative_rows(
    tmp_path, valid_bundle
) -> None:
    payload = canonical(valid_bundle)
    store = IncidentStore(tmp_path)
    created = store.ingest(valid_bundle, payload).record
    store.close()

    reopened = IncidentStore(tmp_path)

    record, restored = reopened.get_artifact("bundle-1")
    assert restored == payload
    assert record == created
    with sqlite3.connect(reopened.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18


def test_v11_catalog_adds_project_lifecycle_columns(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-migrated", display_name="Migrated tenant")
    store.ingest(valid_bundle, canonical(valid_bundle), project_id="tenant-migrated")
    store.close()

    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE projects DROP COLUMN lifecycle_state")
        connection.execute("ALTER TABLE projects DROP COLUMN delete_started_at_unix_nano")
        connection.execute("ALTER TABLE projects DROP COLUMN deleted_at_unix_nano")
        connection.execute("PRAGMA user_version = 11")

    migrated = IncidentStore(tmp_path)

    assert migrated.project_lifecycle("tenant-migrated") == "active"
    assert migrated.get_artifact("bundle-1", project_id="tenant-migrated")[1] == canonical(
        valid_bundle
    )
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        assert {
            "lifecycle_state",
            "delete_started_at_unix_nano",
            "deleted_at_unix_nano",
        } <= columns


def test_v12_catalog_adds_project_scoped_cas_cleanup_queue(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE pending_object_deletions")
        connection.execute("PRAGMA user_version = 12")

    migrated = IncidentStore(tmp_path)

    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'pending_object_deletions'"
            ).fetchone()
            is not None
        )


def test_v17_catalog_adds_project_scoped_pending_ingest_journal(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX pending_ingests_project_idx")
        connection.execute("DROP INDEX pending_ingests_digest_idx")
        connection.execute("DROP TABLE pending_ingests")
        connection.execute("PRAGMA user_version = 17")

    migrated = IncidentStore(tmp_path)

    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pending_ingests'"
            ).fetchone()
            is not None
        )
        assert {row[1] for row in connection.execute("PRAGMA table_info(pending_ingests)")} == {
            "intent_id",
            "project_id",
            "object_digest",
            "started_at_unix_nano",
        }


def test_failed_migration_rolls_back_every_catalog_change(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE incidents (
                bundle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                object_digest TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                finality TEXT NOT NULL,
                completeness TEXT NOT NULL,
                framework TEXT,
                created_at_unix_nano TEXT NOT NULL,
                ingested_at_unix_nano INTEGER NOT NULL
            );
            CREATE TABLE analyses (
                bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
                analyzer_version TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                generated_at_unix_nano INTEGER NOT NULL,
                output_json BLOB NOT NULL,
                PRIMARY KEY (bundle_id, analyzer_version, input_digest)
            );
            INSERT INTO analyses VALUES (
                'missing-incident', 'legacy', 'digest', 123, X'7B7D'
            );
            PRAGMA user_version = 2;
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        IncidentStore(tmp_path)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == {"incidents", "analyses"}
        assert {
            row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(analyses)")
        }["generated_at_unix_nano"] == "INTEGER"


def test_newer_catalog_is_refused_without_downgrading_or_mutating_it(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE future_catalog_marker (value TEXT NOT NULL);
            INSERT INTO future_catalog_marker VALUES ('keep-me');
            PRAGMA user_version = 19;
            """
        )

    with pytest.raises(StorageError, match="schema is newer than this binary"):
        IncidentStore(tmp_path)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert connection.execute("SELECT value FROM future_catalog_marker").fetchone()[0] == (
            "keep-me"
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
            ).fetchone()
            is None
        )


def test_catalog_recovers_after_process_exit_during_a_schema_transaction(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE incidents (
                bundle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                object_digest TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                finality TEXT NOT NULL,
                completeness TEXT NOT NULL,
                framework TEXT,
                created_at_unix_nano TEXT NOT NULL,
                ingested_at_unix_nano INTEGER NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )

    interrupted = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; "
                "c = sqlite3.connect(sys.argv[1]); "
                "c.execute('PRAGMA journal_mode = WAL'); "
                "c.execute('BEGIN IMMEDIATE'); "
                "c.execute('ALTER TABLE incidents ADD COLUMN interrupted TEXT'); "
                "c.execute('CREATE TABLE partial_migration(value TEXT)'); "
                "os._exit(17)"
            ),
            str(database),
        ],
        check=False,
    )
    assert interrupted.returncode == 17

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(incidents)")}
        assert "interrupted" not in columns
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_migration'"
            ).fetchone()
            is None
        )

    migrated = IncidentStore(tmp_path)
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18


def test_instance_correlation_key_is_a_stable_backup_component(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    before = store.fingerprint("test", "provider-value")
    store.close()

    restarted = IncidentStore(tmp_path)

    assert restarted.fingerprint("test", "provider-value") == before
    key_path = tmp_path / "instance-correlation.key"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert len(key_path.read_bytes()) == 32


def test_complete_persistence_unit_backup_restores_identity_authority_and_evidence(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    restored_path = tmp_path / "restored"
    bundle = make_valid_bundle(bundle_id="restored-sales-incident")
    payload = canonical(bundle)
    store = IncidentStore(source)
    store.create_project("sales", display_name="Sales")
    store.ingest(bundle, payload, project_id="sales")
    issued = store.issue_api_key("sales", label="restored key")
    connector = store.create_connector(
        "sales",
        provider="vapi",
        secret_ref="env:VAPI_WEBHOOK_SECRET",
        endpoint_id="connector_backup_restore",
    )
    identity_before = store.fingerprint("vapi.call", "provider-call-123")
    delivery_identity = store.fingerprint("vapi.delivery", "delivery-123")
    claim = store.claim_delivery(
        connector,
        delivery_key_hmac=delivery_identity,
        body_sha256="a" * 64,
        event_type="end-of-call-report",
        now_unix_nano=100,
    )
    assert claim.lease_token is not None
    store.complete_delivery(
        claim.receipt_id,
        state="ignored",
        completed_at_unix_nano=200,
        lease_token=claim.lease_token,
    )
    store.close()

    shutil.copytree(source, backup)
    shutil.copytree(backup, restored_path)

    restored = IncidentStore(restored_path)

    assert restored.get_artifact("restored-sales-incident", project_id="sales")[1] == payload
    principal = restored.authenticate_api_key(issued.credential)
    assert principal is not None
    assert principal.project_id == "sales"
    assert restored.get_connector(connector.endpoint_id) == connector
    assert restored.fingerprint("vapi.call", "provider-call-123") == identity_before
    replay = restored.claim_delivery(
        connector,
        delivery_key_hmac=restored.fingerprint("vapi.delivery", "delivery-123"),
        body_sha256="a" * 64,
        event_type="end-of-call-report",
        now_unix_nano=300,
    )
    assert replay.disposition == "replayed"
    assert replay.receipt_id == claim.receipt_id
    assert (restored_path / "instance-correlation.key").read_bytes() == (
        backup / "instance-correlation.key"
    ).read_bytes()


@pytest.mark.parametrize("retain_cas", [True, False])
def test_existing_catalog_without_instance_correlation_key_fails_closed(
    tmp_path, valid_bundle, retain_cas: bool
) -> None:
    store = IncidentStore(tmp_path)
    record = store.ingest(valid_bundle, canonical(valid_bundle)).record
    store.close()
    if not retain_cas:
        store.objects.path_for(record.digest).unlink()
    (tmp_path / "instance-correlation.key").unlink()

    with pytest.raises(StorageError, match=r"restore instance-correlation\.key"):
        IncidentStore(tmp_path)


def test_tombstone_only_catalog_without_instance_correlation_key_fails_closed(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.purge(valid_bundle.profile.manifest.bundle_id)
    store.close()

    assert not store.objects.has_objects()
    (tmp_path / "instance-correlation.key").unlink()

    with pytest.raises(StorageError, match=r"restore instance-correlation\.key"):
        IncidentStore(tmp_path)


def test_uncertain_correlation_catalog_read_does_not_regenerate_missing_key(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    import earshot.storage as storage_module

    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.purge(valid_bundle.profile.manifest.bundle_id)
    store.close()
    key_path = tmp_path / "instance-correlation.key"
    key_path.unlink()
    original_connect = storage_module.sqlite3.connect

    def fail_read_only_catalog_open(database, *args, **kwargs):
        if isinstance(database, str) and "?mode=ro" in database:
            raise sqlite3.OperationalError("simulated concurrent catalog maintenance")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(storage_module.sqlite3, "connect", fail_read_only_catalog_open)

    with pytest.raises(StorageError, match=r"restore instance-correlation\.key"):
        IncidentStore(tmp_path)
    assert not key_path.exists()
