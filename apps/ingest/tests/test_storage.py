from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from earshot.codec import encode_incident_protobuf
from earshot.contract import AnalysisProjections, DerivedAnalysis, ExportPolicy, RetentionPolicy
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
    return DerivedAnalysis(
        analyzer_name="test.analyzer",
        analyzer_version=version,
        input_sha256=store.get_record(bundle_id).digest,
        generated_at_unix_nano="1800000000000000000",
        projections=AnalysisProjections(limitations=(marker,)),
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


def test_corrupt_analysis_json_is_detected(tmp_path, valid_bundle) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, canonical(valid_bundle))
    store.put_analysis("bundle-1", "1", analysis_value(store, "bundle-1", "1", "ok"))
    with sqlite3.connect(store.database_path, uri=store._database_uri) as connection:
        connection.execute("UPDATE analyses SET output_json = ?", (b"{not-json",))
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


def test_list_never_returns_expired_incidents_and_purges_their_evidence(tmp_path) -> None:
    bundle = with_metadata_retention(
        make_valid_bundle(bundle_id="expired-list"),
        RetentionPolicy(expires_at_unix_nano="0"),
    )
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, canonical(bundle))
    object_path = store.objects.path_for(result.record.digest)

    assert store.list_incidents().items == ()
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
            """
        )
    store = IncidentStore(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        column = next(
            row
            for row in connection.execute("PRAGMA table_info(analyses)")
            if row[1] == "generated_at_unix_nano"
        )
        assert column[2].upper() == "TEXT"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_plaintext_tombstone_id_is_migrated_to_a_digest(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.close()
    database = tmp_path / "earshot.sqlite3"
    sensitive_id = "alice.example.com"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TABLE tombstones;
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
            "SELECT bundle_id_sha256, purged_at_unix_nano FROM tombstones"
        ).fetchone()
        assert row == (hashlib.sha256(sensitive_id.encode()).hexdigest(), 123)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
