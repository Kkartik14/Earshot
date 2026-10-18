from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from earshot.api import ApiConfig, create_app
from earshot.capture.calls import CaptureCallRegistry
from earshot.capture.durable import CaptureSidecar, journal_path, sidecar_path, write_sidecar
from earshot.codec import encode_incident_protobuf
from earshot.storage import IncidentStore, ProjectInactiveError, StorageError
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.integration


def _write_pending_capture(directory, project_id: str, call_key: str) -> CaptureSidecar:
    sidecar = CaptureSidecar(
        project_id=project_id,
        call_key=call_key,
        applied=1,
        digests={1: "a" * 64},
        turn_sequence=0,
        first_observed_ms=None,
        last_observed_ms=None,
        lossy=False,
        finalized=False,
    )
    write_sidecar(directory, sidecar)
    journal_path(directory, call_key).write_bytes(b"pending evidence")
    return sidecar


def test_project_deletion_is_repeatable_and_keeps_another_project_intact(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-a", display_name="Tenant A")
    store.create_project("tenant-b", display_name="Tenant B")
    bundle_a = make_valid_bundle(bundle_id="tenant-a-evidence")
    bundle_b = make_valid_bundle(bundle_id="tenant-b-evidence")
    store.ingest(bundle_a, encode_incident_protobuf(bundle_a), project_id="tenant-a")
    store.ingest(bundle_b, encode_incident_protobuf(bundle_b), project_id="tenant-b")
    key_a = store.issue_api_key("tenant-a", label="to be removed")
    key_b = store.issue_api_key("tenant-b", label="to keep")
    connector = store.create_connector(
        "tenant-a",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_SECRET",
        endpoint_id="delete_connector_0001",
    )
    store.set_external_reference(
        bundle_a.profile.manifest.bundle_id,
        project_id="tenant-a",
        namespace="platform",
        record_type="call",
        external_id="call-a",
    )
    claim = store.claim_delivery(
        connector,
        delivery_key_hmac=store.fingerprint("delivery:test", "pending-a"),
        body_sha256="a" * 64,
        event_type="finalized",
        now_unix_nano=1_000_000_000,
    )

    assert store.delete_project_data("tenant-a") is True
    assert store.delete_project_data("tenant-a") is True
    assert store.project_lifecycle("tenant-a") == "deleted"
    assert store.project_is_active("tenant-a") is False
    assert store.project_is_active("tenant-b") is True
    assert store.authenticate_api_key(key_a.credential) is None
    assert store.authenticate_api_key(key_b.credential) is not None
    assert store.get_artifact(bundle_b.profile.manifest.bundle_id, project_id="tenant-b")[1] == (
        encode_incident_protobuf(bundle_b)
    )
    with pytest.raises(ProjectInactiveError):
        store.get_record(bundle_a.profile.manifest.bundle_id, project_id="tenant-a")
    with pytest.raises(StorageError):
        store.complete_delivery(
            claim.receipt_id,
            state="ignored",
            completed_at_unix_nano=2_000_000_000,
            lease_token=claim.lease_token or 0,
        )

    with sqlite3.connect(store.database_path) as connection:
        for table in (
            "api_keys",
            "connectors",
            "delivery_receipts",
            "external_identities",
            "incident_external_references",
            "incidents",
            "turn_metrics",
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", ("tenant-a",)
                ).fetchone()[0]
                == 0
            )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE project_id = ?", ("tenant-b",)
            ).fetchone()[0]
            == 1
        )


def test_project_deletion_returns_between_bounded_batches(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-a", display_name="Large project")
    store.create_project("tenant-b", display_name="Other project")
    for bundle_id in ("batch-a-one", "batch-a-two"):
        bundle = make_valid_bundle(bundle_id=bundle_id)
        store.ingest(bundle, encode_incident_protobuf(bundle), project_id="tenant-a")
    other = make_valid_bundle(bundle_id="other-project-evidence")
    store.ingest(other, encode_incident_protobuf(other), project_id="tenant-b")

    assert store.delete_project_data("tenant-a", batch_size=1) is False
    assert store.project_lifecycle("tenant-a") == "deleting"
    with pytest.raises(ProjectInactiveError), store.project_write_scope("tenant-a"):
        raise AssertionError("deleting projects must refuse writes before cleanup completes")
    with store._connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM incidents WHERE project_id = ?", ("tenant-a",)
        ).fetchone()[0]
    assert remaining == 1
    assert store.get_record("other-project-evidence", project_id="tenant-b").bundle_id == (
        "other-project-evidence"
    )

    assert store.delete_project_data("tenant-a", batch_size=1) is True
    assert store.project_lifecycle("tenant-a") == "deleted"
    assert store.project_is_active("tenant-b") is True


def test_project_deletion_preserves_unattributed_cas_orphans(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-a", display_name="Tenant A")
    bundle = make_valid_bundle(bundle_id="tenant-a-owned-evidence")
    store.ingest(bundle, encode_incident_protobuf(bundle), project_id="tenant-a")
    orphan_digest, _ = store.objects.put(b"unattributed crash-left evidence")
    orphan_path = store.objects.path_for(orphan_digest)
    assert orphan_path.is_file()

    assert store.delete_project_data("tenant-a") is True

    assert orphan_path.read_bytes() == b"unattributed crash-left evidence"


def test_project_deletion_cleans_object_published_by_interrupted_ingest(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-crashed-ingest", display_name="Interrupted ingest")
    bundle = make_valid_bundle(bundle_id="interrupted-ingest-evidence")
    payload = encode_incident_protobuf(bundle)
    digest = hashlib.sha256(payload).hexdigest()
    object_path = store.objects.path_for(digest)
    original_put = store.objects.put

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_after_publish(value: bytes):
        original_put(value)
        raise SimulatedProcessCrash

    monkeypatch.setattr(store.objects, "put", crash_after_publish)
    with pytest.raises(SimulatedProcessCrash):
        store.ingest(bundle, payload, project_id="tenant-crashed-ingest")
    assert object_path.is_file()
    assert store.delete_project_data("tenant-crashed-ingest") is True
    assert not object_path.exists()


def test_startup_cleans_object_from_interrupted_ingest(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-startup-recovery", display_name="Startup recovery")
    bundle = make_valid_bundle(bundle_id="startup-recovery-evidence")
    payload = encode_incident_protobuf(bundle)
    object_path = store.objects.path_for(hashlib.sha256(payload).hexdigest())
    original_put = store.objects.put

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_after_publish(value: bytes):
        original_put(value)
        raise SimulatedProcessCrash

    monkeypatch.setattr(store.objects, "put", crash_after_publish)
    with pytest.raises(SimulatedProcessCrash):
        store.ingest(bundle, payload, project_id="tenant-startup-recovery")
    assert object_path.is_file()
    store.close()

    reopened = IncidentStore(tmp_path)
    assert reopened.project_lifecycle("tenant-startup-recovery") == "active"
    assert not object_path.exists()


def test_legacy_pending_deletion_requires_explicit_orphan_cleanup(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-legacy", display_name="Legacy pending deletion")
    bundle = make_valid_bundle(bundle_id="legacy-deletion-evidence")
    record = store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id="tenant-legacy",
    ).record
    object_path = store.objects.path_for(record.digest)
    assert object_path.is_file()

    store.begin_project_deletion("tenant-legacy")
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM incidents WHERE project_id = ?",
            ("tenant-legacy",),
        )
        connection.execute("DROP INDEX IF EXISTS pending_object_deletions_project_idx")
        connection.execute("DROP TABLE IF EXISTS pending_object_deletions")
        connection.execute("DROP TABLE IF EXISTS legacy_deletion_reviews")
        connection.execute("PRAGMA user_version = 12")
        connection.commit()
    store.close()

    migrated = IncidentStore(tmp_path)
    assert migrated.delete_project_data("tenant-legacy") is False
    assert migrated.project_lifecycle("tenant-legacy") == "deleting"
    assert object_path.is_file()

    import earshot.storage as storage_module

    original_fsync_directory = storage_module._fsync_directory

    def fail_shard_fsync(path) -> None:
        if path == object_path.parent:
            raise OSError("simulated directory fsync failure")
        original_fsync_directory(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(storage_module, "_fsync_directory", fail_shard_fsync)
        with pytest.raises(OSError, match="simulated directory fsync failure"):
            migrated.cleanup_unreferenced_objects()
        assert not object_path.exists()
        with migrated._connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM legacy_deletion_reviews WHERE project_id = ?",
                    ("tenant-legacy",),
                ).fetchone()[0]
                == 1
            )
        with pytest.raises(OSError, match="simulated directory fsync failure"):
            migrated.cleanup_unreferenced_objects()

    assert migrated.cleanup_unreferenced_objects() == 0


def test_global_object_sweep_preflights_the_whole_tree_before_unlinking(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    digest = "00" + "1" * 62
    orphan = store.objects.path_for(digest)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan object")
    later_shard = store.objects.root / "ff"
    later_shard.mkdir()
    invalid = later_shard / "not-a-content-addressed-object"
    invalid.symlink_to(orphan)

    original_iterdir = Path.iterdir

    def adversarial_order(directory: Path):
        if directory == store.objects.root:
            return iter((orphan.parent, later_shard))
        if directory == orphan.parent:
            return iter((orphan,))
        if directory == later_shard:
            return iter((invalid,))
        return original_iterdir(directory)

    monkeypatch.setattr(Path, "iterdir", adversarial_order)

    with pytest.raises(StorageError, match="CAS entry is not a regular"):
        store.cleanup_unreferenced_objects()

    assert orphan.read_bytes() == b"orphan object"


def test_global_orphan_cleanup_refuses_symlinked_cas_shard(tmp_path) -> None:
    store = IncidentStore(tmp_path / "store")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / ("ab" * 31)
    sentinel.write_bytes(b"outside the CAS root")
    shard = store.objects.root / "aa"
    shard.symlink_to(external, target_is_directory=True)

    with pytest.raises(StorageError, match="CAS shard is not a real directory"):
        store.cleanup_unreferenced_objects()

    assert sentinel.read_bytes() == b"outside the CAS root"


def test_project_deletion_waits_for_cas_shard_directory_fsync(tmp_path, monkeypatch) -> None:
    import earshot.storage as storage_module

    store = IncidentStore(tmp_path)
    store.create_project("tenant-sync", display_name="Fsync pending")
    bundle = make_valid_bundle(bundle_id="fsync-pending-evidence")
    record = store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id="tenant-sync",
    ).record
    object_path = store.objects.path_for(record.digest)
    original_fsync_directory = storage_module._fsync_directory

    def fail_shard_fsync(path) -> None:
        if path == object_path.parent:
            raise OSError("simulated directory fsync failure")
        original_fsync_directory(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(storage_module, "_fsync_directory", fail_shard_fsync)
        assert store.delete_project_data("tenant-sync") is False
        assert not object_path.exists()
        assert store.delete_project_data("tenant-sync") is False

    assert store.delete_project_data("tenant-sync") is True


def test_project_deletion_reports_cleanup_pending_and_retries_object_removal(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-pending", display_name="Pending cleanup")
    bundle = make_valid_bundle(bundle_id="pending-evidence")
    store.ingest(bundle, encode_incident_protobuf(bundle), project_id="tenant-pending")
    original_unlink = __import__("pathlib").Path.unlink
    object_root = store.objects.root

    def fail_object_unlink(path, *args, **kwargs):
        if path.is_relative_to(object_root):
            raise PermissionError("simulated object lock")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr("pathlib.Path.unlink", fail_object_unlink)
        complete = store.delete_project_data("tenant-pending")

    assert complete is False
    assert store.project_lifecycle("tenant-pending") == "deleting"
    store.close()
    store = IncidentStore(tmp_path)
    assert store.delete_project_data("tenant-pending") is True
    assert store.project_lifecycle("tenant-pending") == "deleted"


def test_project_deletion_batches_owned_delivery_receipts(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-receipts", display_name="Receipts")
    connector = store.create_connector(
        "tenant-receipts",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_SECRET",
        endpoint_id="receipt_batch_endpoint",
    )
    for index in range(3):
        store.claim_delivery(
            connector,
            delivery_key_hmac=store.fingerprint("delivery:test", f"receipt-{index}"),
            body_sha256=str(index) * 64,
            event_type="finalized",
            now_unix_nano=index + 1,
        )

    assert store.delete_project_data("tenant-receipts", batch_size=1) is False
    with store._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM delivery_receipts WHERE project_id = ?",
                ("tenant-receipts",),
            ).fetchone()[0]
            == 2
        )

    assert store.delete_project_data("tenant-receipts", batch_size=1) is False
    with store._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM delivery_receipts WHERE project_id = ?",
                ("tenant-receipts",),
            ).fetchone()[0]
            == 1
        )

    assert store.delete_project_data("tenant-receipts", batch_size=1) is True


def test_project_write_scope_rejects_mutations_after_deletion_starts(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("tenant-write", display_name="Write fence")
    assert store.delete_project_data("tenant-write") is True

    with pytest.raises(StorageError), store.project_write_scope("tenant-write"):
        raise AssertionError("inactive project write scope must not be entered")


def test_project_deletion_http_api_rejects_operator_api_keys(tmp_path) -> None:
    from fastapi.testclient import TestClient

    store = IncidentStore(tmp_path)
    store.create_project("tenant-operator", display_name="Operator project")
    key = store.issue_api_key("tenant-operator", label="ordinary project key")
    client = TestClient(create_app(store=store, config=ApiConfig()))

    response = client.delete(
        "/v1/projects/tenant-operator",
        headers={"Authorization": f"Bearer {key.credential}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EARSHOT_HOSTED_AUTH_REQUIRED"
    assert store.project_lifecycle("tenant-operator") == "active"


def test_capture_deletion_removes_only_the_target_projects_pending_call_files(
    tmp_path,
) -> None:
    from earshot.live import LiveSessionRegistry

    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=tmp_path)
    for project_id, call_key in (("tenant-a", "call-a"), ("tenant-b", "call-b")):
        _write_pending_capture(tmp_path, project_id, call_key)

    assert registry.drop_project("tenant-a") is True
    assert not sidecar_path(tmp_path, "call-a").exists()
    assert not journal_path(tmp_path, "call-a").exists()
    assert sidecar_path(tmp_path, "call-b").is_file()
    assert journal_path(tmp_path, "call-b").read_bytes() == b"pending evidence"


def test_capture_deletion_retries_directory_fsync_after_files_are_gone(
    tmp_path, monkeypatch
) -> None:
    from earshot.capture import calls as capture_calls
    from earshot.live import LiveSessionRegistry

    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=tmp_path)
    _write_pending_capture(tmp_path, "tenant-a", "call-a")
    original_fsync = capture_calls.os.fsync

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(capture_calls.os, "fsync", fail_fsync)
    assert registry.drop_project("tenant-a") is False
    assert not sidecar_path(tmp_path, "call-a").exists()
    assert not journal_path(tmp_path, "call-a").exists()
    assert registry.drop_project("tenant-a") is False

    monkeypatch.setattr(capture_calls.os, "fsync", original_fsync)
    assert registry.drop_project("tenant-a") is True
    assert not sidecar_path(tmp_path, "call-a").exists()
    assert not journal_path(tmp_path, "call-a").exists()


def test_sealed_journal_removal_retries_directory_fsync_after_unlink(tmp_path, monkeypatch) -> None:
    from earshot.capture import durable as capture_durable

    journal = journal_path(tmp_path, "sealed-call")
    journal.write_bytes(b"sealed journal")
    original_fsync = capture_durable._fsync_directory
    attempts = 0

    def fail_once(directory: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated directory fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(capture_durable, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        capture_durable.remove_sealed_journal(tmp_path, "sealed-call")
    assert not journal.exists()

    capture_durable.remove_sealed_journal(tmp_path, "sealed-call")
    assert attempts == 2


def test_capture_deletion_stays_pending_when_a_sidecar_cannot_identify_ownership(
    tmp_path,
) -> None:
    from earshot.live import LiveSessionRegistry

    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=tmp_path)
    sidecar = _write_pending_capture(tmp_path, "tenant-a", "call-a")
    ledger = sidecar_path(tmp_path, "call-a")
    journal = journal_path(tmp_path, "call-a")
    ledger.write_bytes(b"not valid sidecar data")

    assert registry.drop_project("tenant-a") is False
    assert ledger.is_file()
    assert journal.read_bytes() == b"pending evidence"

    write_sidecar(tmp_path, sidecar)
    assert registry.drop_project("tenant-a") is True
    assert not ledger.exists()
    assert not journal.exists()


def test_capture_deletion_stays_pending_for_an_orphan_journal(tmp_path) -> None:
    from earshot.live import LiveSessionRegistry

    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=tmp_path)
    orphan = journal_path(tmp_path, "call-without-sidecar")
    orphan.write_bytes(b"pending evidence")

    assert registry.drop_project("tenant-a") is False
    assert orphan.read_bytes() == b"pending evidence"


def test_capture_deletion_stays_pending_when_its_journal_path_is_not_a_directory(
    tmp_path,
) -> None:
    from earshot.live import LiveSessionRegistry

    capture_path = tmp_path / "capture-files"
    capture_path.write_bytes(b"capture directory is unavailable")
    registry = CaptureCallRegistry(LiveSessionRegistry(), journal_dir=capture_path)

    assert registry.drop_project("tenant-a") is False
