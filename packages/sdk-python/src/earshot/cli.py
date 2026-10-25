"""Command-line entry point for local validation, storage, and serving.

Commands that open the incident store import ``earshot.storage`` only when they
run: the store needs the ``server`` extra, while validation, query, export and
checkpoint commands work on a base install.

Every store command takes ``--database-url`` (default ``EARSHOT_DATABASE_URL``).
Without it the catalog is SQLite in ``--data-dir``; with a ``postgresql://`` URL it
is that database, and the command also needs the ``postgres`` extra and the
correlation key (``EARSHOT_CORRELATION_KEY`` or ``EARSHOT_CORRELATION_KEY_FILE``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .checkpoint import (
    AssemblyError,
    JournalUnreadableError,
    assemble_incident,
    summarize_directory,
)
from .codec import (
    IncidentCodecError,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from .exporters import span_count
from .exporters.push import OtlpHttpExporter
from .exporters.registry import export_incident, exporter_names
from .privacy import ExportPolicyError, assert_export_allowed
from .query import EvidenceQuery, compare_incidents
from .validation import assert_valid_incident, validate_incident


def _data_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("EARSHOT_DATA_DIR", ".earshot"))


def _database_url(arguments: argparse.Namespace) -> str | None:
    """The catalog URL, or ``None`` for SQLite in the data directory."""

    return (
        getattr(arguments, "database_url", None) or os.environ.get("EARSHOT_DATABASE_URL") or None
    )


def _add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir")
    parser.add_argument(
        "--database-url",
        help=(
            "postgresql:// catalog URL (default: EARSHOT_DATABASE_URL; without "
            "either, SQLite in the data directory)"
        ),
    )


def _storage() -> Any:
    """Import storage for a command that opens the store."""

    from . import storage

    return storage


def _open_store(arguments: argparse.Namespace, *, auto_migrate: bool = True) -> Any:
    """Open the store; it refuses with a stable code when the server extra is missing."""

    return _storage().IncidentStore(
        _data_dir(arguments.data_dir),
        database_url=_database_url(arguments),
        auto_migrate=auto_migrate,
    )


def _project_id(arguments: argparse.Namespace) -> str:
    project = arguments.project
    return _storage().DEFAULT_PROJECT_ID if project is None else project


def _decode_file(path: Path, *, validate: bool = True):
    payload = path.read_bytes()
    if path.suffix.lower() in {".json", ".jsonl"}:
        return decode_incident_json(payload, validate=validate)
    return decode_incident_protobuf(payload, validate=validate)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _validate_command(arguments: argparse.Namespace) -> int:
    bundle = _decode_file(Path(arguments.path), validate=False)
    report = validate_incident(bundle)
    canonical_digest = None
    if report.ok:
        canonical_digest = hashlib.sha256(encode_incident_protobuf(bundle)).hexdigest()
    _print_json(
        {
            "valid": report.ok,
            "bundle_id": bundle.profile.manifest.bundle_id if report.ok else None,
            "session_id": bundle.profile.manifest.session_id if report.ok else None,
            "canonical_sha256": canonical_digest,
            "issues": [issue.model_dump(mode="json") for issue in report.issues],
        }
    )
    return 0 if report.ok else 1


def _ingest_command(arguments: argparse.Namespace) -> int:
    bundle = _decode_file(Path(arguments.path))
    report = validate_incident(bundle)
    if not report.ok:
        _print_json(
            {
                "valid": False,
                "issues": [issue.model_dump(mode="json") for issue in report.errors],
            }
        )
        return 1
    store = _open_store(arguments)
    result = store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id=_project_id(arguments),
    )
    value = result.record.as_dict()
    value["created"] = result.created
    _print_json(value)
    return 0


# ``list`` filters on projected columns (retention deadline, export flag). Without
# a server re-projecting in the background, it brings stale incidents up to date
# first, for at most this long.
LIST_REPROJECTION_BUDGET_SECONDS = 10.0


def _list_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    convergence = store.converge_projections(time_budget_seconds=LIST_REPROJECTION_BUDGET_SECONDS)
    if convergence.pending:
        print(
            f"earshot: {convergence.pending} incidents still have projections from an "
            "older release; run 'earshot db reproject'",
            file=sys.stderr,
        )
    page = store.list_incidents(
        project_id=_project_id(arguments),
        session_id=arguments.session_id,
        limit=arguments.limit,
        cursor=arguments.cursor,
        destination="local_cli",
    )
    _print_json(
        {
            "items": [item.as_dict() for item in page.items],
            "next_cursor": page.next_cursor,
        }
    )
    return 0


def _show_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    _, payload = store.get_artifact(arguments.bundle_id, project_id=_project_id(arguments))
    bundle = decode_incident_protobuf(payload)
    assert_export_allowed(bundle, "local_cli")
    if arguments.format == "protobuf":
        sys.stdout.buffer.write(payload)
        return 0
    sys.stdout.buffer.write(encode_incident_json(bundle, indent=2))
    sys.stdout.buffer.write(b"\n")
    return 0


def _purge_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    try:
        store.purge(arguments.bundle_id, project_id=_project_id(arguments))
    except _storage().IncidentBelongsToCallError as error:
        # A call's revisions go with their call.
        print(
            f"earshot: {error.code}: {error}: earshot call purge {error.call_id}",
            file=sys.stderr,
        )
        return 2
    _print_json({"bundle_id": arguments.bundle_id, "purged": True})
    return 0


def _call_purge_command(arguments: argparse.Namespace) -> int:
    """Purge one call and all its evidence, as ``DELETE /v1/calls/{id}`` does.

    The call lifecycle timings and the blob store come from the same settings the
    server reads (``EARSHOT_CALL_*``, ``EARSHOT_BLOB_URL``).
    """

    from .blobstore import open_blob_store
    from .server_config import load_server_settings
    from .storage_call_purge import CallNotFoundError, CallPurger
    from .storage_calls import CallStore
    from .storage_recordings import RecordingStore

    settings = load_server_settings(
        os.environ,
        data_dir=None if arguments.data_dir is None else Path(arguments.data_dir),
        database_url=arguments.database_url,
    )
    store = _open_store(arguments)
    calls = CallStore(store, timings=settings.call_timings())
    recordings = RecordingStore(store, calls=calls, blobs=open_blob_store(settings.blob_url))
    try:
        CallPurger(store, calls=calls, recordings=recordings).purge_call(
            _project_id(arguments), arguments.call_id
        )
    except CallNotFoundError as error:
        print(f"earshot: {error.code}: {error}", file=sys.stderr)
        return 2
    _print_json({"call_id": arguments.call_id, "purged": True})
    return 0


def _cleanup_unreferenced_objects_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    removed = store.cleanup_unreferenced_objects()
    _print_json(
        {
            "scope": "all projects in the data directory",
            "removed_unreferenced_objects": removed,
        }
    )
    return 0


def _project_create_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    project = store.create_project(arguments.project_id, display_name=arguments.display_name)
    _print_json(
        {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "created_at_unix_nano": project.created_at_unix_nano,
        }
    )
    return 0


def _project_set_capture_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    project = store.set_capture_preset(arguments.project, arguments.preset)
    _print_json({"project_id": project.project_id, "capture_preset": project.capture_preset})
    return 0


def _project_show_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    project = store.get_project(arguments.project_id)
    if project is None:
        print(f"earshot: project {arguments.project_id} does not exist", file=sys.stderr)
        return 2
    _print_json(
        {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "created_at_unix_nano": project.created_at_unix_nano,
            "lifecycle_state": project.lifecycle_state,
            "capture_preset": project.capture_preset,
        }
    )
    return 0


def _api_key_issue_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    issued = store.issue_api_key(arguments.project, label=arguments.label)
    _print_json(
        {
            "project_id": issued.project_id,
            "key_id": issued.key_id,
            "label": issued.label,
            "credential": issued.credential,
            "warning": "credential is shown once; store it securely",
        }
    )
    return 0


def _api_key_revoke_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    store.revoke_api_key(arguments.project, arguments.key_id)
    _print_json(
        {
            "project_id": arguments.project,
            "key_id": arguments.key_id,
            "revoked": True,
        }
    )
    return 0


def _connector_create_command(arguments: argparse.Namespace) -> int:
    store = _open_store(arguments)
    connector = store.create_connector(
        arguments.project,
        provider=arguments.provider,
        secret_ref=f"env:{arguments.secret_env}",
    )
    _print_json(
        {
            "endpoint_id": connector.endpoint_id,
            "project_id": connector.project_id,
            "provider": connector.provider,
            "secret_ref": connector.secret_ref,
            "hook_path": f"/hooks/v1/connectors/{connector.endpoint_id}",
        }
    )
    return 0


def _query_command(arguments: argparse.Namespace) -> int:
    bundle = _decode_file(Path(arguments.path))
    query = EvidenceQuery(bundle)
    if arguments.turn is not None:
        _print_json(query.known_about_turn(arguments.turn).as_dict())
    else:
        _print_json(query.summary().as_dict())
    return 0


def _contradictions_command(arguments: argparse.Namespace) -> int:
    bundle = _decode_file(Path(arguments.path))
    query = EvidenceQuery(bundle)
    _print_json({"contradictions": [item.as_dict() for item in query.contradictions()]})
    return 0


def _diff_command(arguments: argparse.Namespace) -> int:
    incident = _decode_file(Path(arguments.incident))
    known_good = _decode_file(Path(arguments.known_good))
    _print_json(compare_incidents(incident, known_good).as_dict())
    return 0


def _parse_headers(pairs: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ValueError("headers must be supplied as KEY=VALUE")
        headers[key.strip()] = value
    return headers


def _export_command(arguments: argparse.Namespace) -> int:
    bundle = _decode_file(Path(arguments.path))
    # Selecting by name is what makes a registered user exporter usable here
    # without a CLI change; the registry enforces the declared export restriction
    # against that exporter's destination, exactly as ``show`` does for its own.
    document = export_incident(bundle, format=arguments.format)

    if arguments.out is not None:
        Path(arguments.out).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if arguments.push_otlp is not None:
        exporter = OtlpHttpExporter(arguments.push_otlp, headers=_parse_headers(arguments.header))
        result = exporter.export(document)
        _print_json(
            {
                "endpoint": exporter.endpoint,
                "error": result.error,
                "ok": result.ok,
                "retryable": result.retryable,
                "spans": result.spans,
                "status": result.status,
            }
        )
        return 0 if result.ok else 1

    if arguments.out is not None:
        _print_json(
            {"format": arguments.format, "out": str(arguments.out), "spans": span_count(document)}
        )
        return 0

    _print_json(document)
    return 0


def _checkpoint_key() -> bytes | None:
    from .server_config import checkpoint_key_from_environment

    return checkpoint_key_from_environment()


def _checkpoints_list_command(arguments: argparse.Namespace) -> int:
    summaries = summarize_directory(Path(arguments.checkpoint_dir), key=_checkpoint_key())
    _print_json(
        {
            "journals": [
                {
                    "path": str(summary.path),
                    "journal_id": summary.journal_id,
                    "session_id": summary.session_id,
                    "bundle_id": summary.bundle_id,
                    "state": summary.state,
                    "last_sequence": summary.last_sequence,
                    "torn_tail_bytes": summary.torn_tail_bytes,
                    "total_bytes": summary.total_bytes,
                }
                for summary in summaries
            ]
        }
    )
    return 0


def _select_journal(arguments: argparse.Namespace) -> Path:
    if arguments.journal is not None:
        return Path(arguments.journal)
    if arguments.checkpoint_dir is None:
        raise ValueError("recover requires --journal or --checkpoint-dir")
    matches = [
        summary
        for summary in summarize_directory(Path(arguments.checkpoint_dir), key=_checkpoint_key())
        if arguments.session_id is None or summary.session_id == arguments.session_id
    ]
    if not matches:
        raise ValueError("no journal matches the requested session")
    if len(matches) > 1:
        # Refuse to guess: recovering the wrong session silently would be worse
        # than making the operator name the one they mean.
        raise ValueError("more than one journal matches; pass --session-id or --journal")
    return matches[0].path


def _recover_command(arguments: argparse.Namespace) -> int:
    """Reconstruct an incident from a checkpoint journal.

    Recovering a finalized journal reproduces the artifact ``close()`` produced,
    byte for byte, which is why a duplicate ingest deduplicates instead of
    conflicting. When the close was never observed the artifact is explicitly
    provisional and says so in ``manifest.recovery``.
    """

    result = assemble_incident(
        _select_journal(arguments),
        key=_checkpoint_key(),
        best_effort=arguments.best_effort,
        bundle_id_suffix=arguments.bundle_id_suffix,
    )
    assert_valid_incident(result.bundle)
    report = result.report
    payload = encode_incident_protobuf(result.bundle)
    created = None
    if arguments.ingest:
        store = _open_store(arguments)
        ingested = store.ingest(result.bundle, payload, project_id=_project_id(arguments))
        created = ingested.created
    if arguments.out is not None:
        Path(arguments.out).write_bytes(encode_incident_json(result.bundle, indent=2) + b"\n")
    _print_json(
        {
            "bundle_id": report.bundle_id,
            "session_id": report.session_id,
            "journal_id": report.journal_id,
            "close_observed": report.close_observed,
            "counter_mismatch": report.counter_mismatch,
            "created": created,
            "canonical_sha256": hashlib.sha256(payload).hexdigest(),
            "discarded_records": report.discarded_records,
            "finality": result.bundle.profile.manifest.finality,
            "journal_complete": report.journal_complete,
            "last_sequence": report.last_sequence,
            "out": None if arguments.out is None else str(arguments.out),
            "stop_reason": report.stop_reason,
            "torn_tail_bytes": report.torn_tail_bytes,
            "unfinished_operations": report.unfinished_operations,
        }
    )
    return 0


def _db_current_command(arguments: argparse.Namespace) -> int:
    _print_json(
        _storage().describe_catalog_revision(
            _data_dir(arguments.data_dir), database_url=_database_url(arguments)
        )
    )
    return 0


def _db_upgrade_command(arguments: argparse.Namespace) -> int:
    storage = _storage()
    data_dir = _data_dir(arguments.data_dir)
    database_url = _database_url(arguments)
    before = storage.describe_catalog_revision(data_dir, database_url=database_url)
    # Opening the store is the upgrade: it runs under the store lock, exactly as
    # a server start would.
    _open_store(arguments).close()
    after = storage.describe_catalog_revision(data_dir, database_url=database_url)
    _print_json(
        {
            "head": after["head"],
            "revision": after["revision"],
            "revision_before": before["revision"],
        }
    )
    return 0


def _db_reproject_command(arguments: argparse.Namespace) -> int:
    """Re-project every stale incident now, without a running server."""

    store = _open_store(arguments)
    if arguments.all:
        store.mark_projections_stale()
    convergence = store.converge_projections()
    _print_json(
        {
            "failed": list(convergence.failed),
            "projections_pending": convergence.pending,
            "reprojected": convergence.reprojected,
        }
    )
    return 1 if convergence.failed else 0


def _prices_add_command(arguments: argparse.Namespace) -> int:
    """Append a price: the project's own, or (``--builtin``) a built-in for every
    project. Each insert bumps its scope's price version, so the projections it may
    change are repriced by a running server."""

    from .storage_prices import NewPrice, PriceStore

    store = _open_store(arguments)
    price = PriceStore(store).add(
        None if arguments.builtin else arguments.project,
        NewPrice(
            provider=arguments.provider,
            model=arguments.model,
            unit=arguments.unit,
            price_nano_usd=arguments.price_nano_usd,
            per_quantity=arguments.per_quantity,
            effective_from_unix_nano=arguments.effective_from_unix_nano,
            source_url=arguments.source_url,
            note=arguments.note,
        ),
    )
    _print_json(price.document())
    return 0


def _prices_list_command(arguments: argparse.Namespace) -> int:
    """Every price a project's calls may use: the built-ins and its own, oldest first."""

    from .storage_prices import PriceStore

    prices = PriceStore(_open_store(arguments))
    listed, cursor = [], None
    while True:
        page = prices.list_prices(_project_id(arguments), limit=100, cursor=cursor)
        listed.extend(price.document() for price in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    _print_json(listed)
    return 0


def _db_enable_transcript_index_command(arguments: argparse.Namespace) -> int:
    from .storage_call_reads import TranscriptIndexError, enable_transcript_index

    store = _open_store(arguments)
    try:
        name = enable_transcript_index(store)
    except TranscriptIndexError as error:
        print(f"earshot: {error}", file=sys.stderr)
        return 2
    _print_json({"index": name})
    return 0


def _serve_command(arguments: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .api import create_app  # noqa: F401 - the server extra is importable
    except ModuleNotFoundError:
        print(
            "earshot: server dependencies are not installed; "
            "install 'earshot-observability[server]'",
            file=sys.stderr,
        )
        return 2

    from .server_config import load_server_settings, open_server

    # Flags win over their environment variables; ``serve`` and the ASGI module
    # share this parser and ``open_server`` (docs/configuration.md).
    settings = load_server_settings(
        os.environ,
        data_dir=None if arguments.data_dir is None else Path(arguments.data_dir),
        database_url=arguments.database_url,
        host=arguments.host,
        port=arguments.port,
        web_dir=arguments.web_dir,
        checkpoint_dir=arguments.checkpoint_dir,
        token=arguments.token,
        auth_mode=arguments.auth_mode,
        jwt_issuer=arguments.jwt_issuer,
        jwt_audience=arguments.jwt_audience,
        jwks_url=arguments.jwks_url,
        jwks_ca_file=arguments.jwks_ca_file,
        platform_adapter=arguments.platform_adapter or None,
        behind_tls_proxy=arguments.behind_tls_proxy or None,
        trust_local_network=arguments.trust_local_network or None,
        max_body_bytes=arguments.max_body_bytes,
        max_connector_body_bytes=arguments.max_connector_body_bytes,
        max_connector_deliveries_per_minute=arguments.max_connector_deliveries_per_minute,
        retention_cleanup_interval_seconds=arguments.retention_cleanup_interval_seconds,
        retention_cleanup_batch_size=arguments.retention_cleanup_batch_size,
    )
    # Opening the store migrates the catalog to head, unless --no-auto-migrate
    # asks the server to refuse a catalog that is behind instead.
    app, server_lock = open_server(settings, auto_migrate=not arguments.no_auto_migrate)
    if settings.database_url is not None and server_lock is not None:
        print(f"Earshot database: {server_lock.display_url}", file=sys.stderr)
    else:
        print(f"Earshot data path: {settings.data_dir.resolve()}", file=sys.stderr)
    if settings.checkpoint_dir is not None:
        print(f"Earshot live tail: {settings.checkpoint_dir}", file=sys.stderr)
    print(f"Earshot listening: http://{settings.host}:{settings.port}/", file=sys.stderr)
    if settings.trust_local_network:
        print(
            "Earshot warning: API authentication is disabled "
            "(--trust-local-network); keep this listener on a trusted boundary "
            "such as `docker -p 127.0.0.1:PORT`, never a public interface.",
            file=sys.stderr,
        )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=arguments.log_level,
        access_log=False,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="earshot",
        description="Validate, store, and inspect portable voice-session incidents.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate an incident file")
    validate.add_argument("path")
    validate.set_defaults(handler=_validate_command)

    ingest = commands.add_parser("ingest", help="ingest an incident into the local store")
    ingest.add_argument("path")
    _add_store_arguments(ingest)
    ingest.add_argument("--project", help="project id (default: the built-in default project)")
    ingest.set_defaults(handler=_ingest_command)

    list_parser = commands.add_parser("list", help="list locally stored incidents")
    _add_store_arguments(list_parser)
    list_parser.add_argument("--project", help="project id (default: the built-in default project)")
    list_parser.add_argument("--session-id")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--cursor")
    list_parser.set_defaults(handler=_list_command)

    show = commands.add_parser("show", help="write one stored incident to stdout")
    show.add_argument("bundle_id")
    _add_store_arguments(show)
    show.add_argument("--project", help="project id (default: the built-in default project)")
    show.add_argument("--format", choices=("json", "protobuf"), default="json")
    show.set_defaults(handler=_show_command)

    purge = commands.add_parser("purge", help="physically purge an incident")
    purge.add_argument("bundle_id")
    _add_store_arguments(purge)
    purge.add_argument("--project", help="project id (default: the built-in default project)")
    purge.set_defaults(handler=_purge_command)

    call = commands.add_parser("call", help="manage calls (OTLP and recordings evidence)")
    call_commands = call.add_subparsers(dest="call_command", required=True)
    call_purge = call_commands.add_parser(
        "purge", help="purge a call: its spans, recordings, blobs and revisions"
    )
    call_purge.add_argument("call_id")
    _add_store_arguments(call_purge)
    call_purge.add_argument("--project", help="project id (default: the built-in default project)")
    call_purge.set_defaults(handler=_call_purge_command)

    maintenance = commands.add_parser(
        "maintenance", help="run explicit store-wide maintenance operations"
    )
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    cleanup_objects = maintenance_commands.add_parser(
        "cleanup-unreferenced-objects",
        help="globally remove CAS objects not referenced by any incident",
    )
    _add_store_arguments(cleanup_objects)
    cleanup_objects.add_argument(
        "--confirm-global-object-sweep",
        action="store_true",
        required=True,
        help="confirm a backup exists and every unreferenced object may be removed",
    )
    cleanup_objects.set_defaults(handler=_cleanup_unreferenced_objects_command)

    project = commands.add_parser("project", help="manage project authorization scopes")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_create = project_commands.add_parser("create", help="create a project")
    project_create.add_argument("project_id")
    project_create.add_argument("--display-name", required=True)
    _add_store_arguments(project_create)
    project_create.set_defaults(handler=_project_create_command)
    project_set_capture = project_commands.add_parser(
        "set-capture",
        help="choose what the OTLP receiver keeps of a project's evidence",
    )
    project_set_capture.add_argument("--project", required=True)
    project_set_capture.add_argument(
        "--preset",
        required=True,
        # redacted is listed so that it is refused with its stable code, not as a typo.
        choices=("full", "metadata_only", "redacted"),
        help=(
            "full keeps content; metadata_only drops session keys and the attributes whose "
            "key names it classifies as content or identity (a classifier, not an "
            "allowlist: see docs/privacy.md)"
        ),
    )
    _add_store_arguments(project_set_capture)
    project_set_capture.set_defaults(handler=_project_set_capture_command)
    project_show = project_commands.add_parser("show", help="show a project")
    project_show.add_argument("project_id")
    _add_store_arguments(project_show)
    project_show.set_defaults(handler=_project_show_command)

    api_key = commands.add_parser("api-key", help="manage project API keys")
    api_key_commands = api_key.add_subparsers(dest="api_key_command", required=True)
    api_key_issue = api_key_commands.add_parser("issue", help="issue a project API key")
    api_key_issue.add_argument("--project", required=True)
    api_key_issue.add_argument("--label", required=True)
    _add_store_arguments(api_key_issue)
    api_key_issue.set_defaults(handler=_api_key_issue_command)
    api_key_revoke = api_key_commands.add_parser("revoke", help="revoke a project API key")
    api_key_revoke.add_argument("--project", required=True)
    api_key_revoke.add_argument("--key-id", required=True)
    _add_store_arguments(api_key_revoke)
    api_key_revoke.set_defaults(handler=_api_key_revoke_command)

    connector = commands.add_parser("connector", help="manage hosted-provider connectors")
    connector_commands = connector.add_subparsers(dest="connector_command", required=True)
    connector_create = connector_commands.add_parser(
        "create", help="create a signed webhook endpoint"
    )
    connector_create.add_argument("--project", required=True)
    connector_create.add_argument(
        "--provider", required=True, choices=("elevenlabs", "vapi", "retell", "ringg")
    )
    connector_create.add_argument("--secret-env", required=True)
    _add_store_arguments(connector_create)
    connector_create.set_defaults(handler=_connector_create_command)

    query = commands.add_parser(
        "query", help="query an incident's evidence graph (summary or one turn)"
    )
    query.add_argument("path")
    query.add_argument("--turn", help="report everything known about this turn id")
    query.add_argument(
        "--json",
        action="store_true",
        help="emit structured JSON (the default and only output form)",
    )
    query.set_defaults(handler=_query_command)

    contradictions = commands.add_parser(
        "contradictions", help="detect evidence-linked contradictions in an incident"
    )
    contradictions.add_argument("path")
    contradictions.set_defaults(handler=_contradictions_command)

    diff = commands.add_parser("diff", help="diff an incident against a known-good incident")
    diff.add_argument("incident")
    diff.add_argument("known_good")
    diff.set_defaults(handler=_diff_command)

    export = commands.add_parser(
        "export",
        help="project an incident with a registered exporter and optionally push it",
    )
    export.add_argument("path")
    # Sourced from the registry so a process that registered its own exporter can
    # drive it from the CLI; sorted names keep `--help` stable.
    export.add_argument(
        "--format",
        choices=exporter_names(),
        required=True,
        help="registered exporter name (built in: otlp, openinference)",
    )
    export.add_argument("--out", help="write the projected OTLP/JSON document to this file")
    export.add_argument(
        "--push-otlp",
        metavar="ENDPOINT",
        help="POST the projected document to an OTLP traces endpoint (e.g. Phoenix/Langfuse)",
    )
    export.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="header to send with --push-otlp (repeatable), e.g. authentication",
    )
    export.set_defaults(handler=_export_command)

    checkpoints = commands.add_parser("checkpoints", help="inspect crash-recovery journals")
    checkpoints_commands = checkpoints.add_subparsers(dest="checkpoints_command", required=True)
    checkpoints_list = checkpoints_commands.add_parser(
        "list", help="identify every journal in a checkpoint directory"
    )
    checkpoints_list.add_argument("checkpoint_dir")
    checkpoints_list.set_defaults(handler=_checkpoints_list_command)

    recover = commands.add_parser(
        "recover", help="reconstruct an incident from a checkpoint journal"
    )
    recover.add_argument("--checkpoint-dir")
    recover.add_argument("--journal", help="recover this exact journal file")
    recover.add_argument("--session-id", help="select the journal for this session")
    recover.add_argument("--out", help="write the recovered incident JSON to this file")
    recover.add_argument(
        "--ingest", action="store_true", help="ingest the recovered incident locally"
    )
    _add_store_arguments(recover)
    recover.add_argument("--project", help="project id (default: the built-in default project)")
    recover.add_argument(
        "--best-effort",
        action="store_true",
        help="accept a journal whose replay disagrees with the recorder's own totals",
    )
    recover.add_argument(
        "--bundle-id-suffix",
        help=(
            "suffix the recovered bundle id (for example .r2) when a previous "
            "recovery of this journal is already stored under a different SDK version"
        ),
    )
    recover.set_defaults(handler=_recover_command)

    serve = commands.add_parser("serve", help="run the local ingest API")
    _add_store_arguments(serve)
    serve.add_argument("--host", help="listener address (EARSHOT_HOST; default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="listener port (EARSHOT_PORT; default 4319)")
    serve.add_argument(
        "--web-dir",
        help="serve a built viewer SPA from this directory at the site root (EARSHOT_WEB_DIR)",
    )
    serve.add_argument(
        "--checkpoint-dir",
        default=None,
        help=(
            "follow the crash-recovery journals in this directory and expose them "
            "under /v1/live (EARSHOT_CHECKPOINT_DIR); live sessions are never listed "
            "as incidents"
        ),
    )
    serve.add_argument("--token", help="legacy operator bearer token (EARSHOT_TOKEN)")
    serve.add_argument(
        "--auth-mode",
        choices=("operator", "hosted_jwt"),
        help="operator (default) or hosted_jwt (EARSHOT_AUTH_MODE)",
    )
    serve.add_argument("--jwt-issuer", help="EARSHOT_JWT_ISSUER")
    serve.add_argument("--jwt-audience", help="EARSHOT_JWT_AUDIENCE")
    serve.add_argument("--jwks-url", help="EARSHOT_JWKS_URL")
    serve.add_argument("--jwks-ca-file", help="EARSHOT_JWKS_CA_FILE")
    serve.add_argument(
        "--platform-adapter",
        action="store_true",
        help=(
            "mount the optional Platform Observe response adapter (hosted_jwt only; "
            "EARSHOT_PLATFORM_ADAPTER_ENABLED)"
        ),
    )
    serve.add_argument(
        "--behind-tls-proxy",
        action="store_true",
        help=(
            "confirm that a same-host proxy exposes the loopback listener over HTTPS "
            "(EARSHOT_BEHIND_TLS_PROXY)"
        ),
    )
    serve.add_argument(
        "--trust-local-network",
        action="store_true",
        help=(
            "serve the API without authentication on a listener confined to a "
            "trusted local boundary (e.g. `docker -p 127.0.0.1:PORT`); never on "
            "a public interface (EARSHOT_TRUST_LOCAL_NETWORK)"
        ),
    )
    serve.add_argument("--max-body-bytes", type=int, help="EARSHOT_MAX_BODY_BYTES")
    serve.add_argument(
        "--max-connector-body-bytes", type=int, help="EARSHOT_MAX_CONNECTOR_BODY_BYTES"
    )
    serve.add_argument(
        "--max-connector-deliveries-per-minute",
        type=int,
        help="EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE",
    )
    serve.add_argument(
        "--retention-cleanup-interval-seconds",
        type=float,
        help="EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS",
    )
    serve.add_argument(
        "--retention-cleanup-batch-size", type=int, help="EARSHOT_RETENTION_CLEANUP_BATCH_SIZE"
    )
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--no-auto-migrate",
        action="store_true",
        help="refuse to start when the catalog schema is behind instead of migrating it",
    )
    serve.set_defaults(handler=_serve_command)

    db = commands.add_parser("db", help="inspect or migrate the catalog schema")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_current = db_commands.add_parser(
        "current", help="report the catalog's schema revision without changing it"
    )
    _add_store_arguments(db_current)
    db_current.set_defaults(handler=_db_current_command)
    db_upgrade = db_commands.add_parser(
        "upgrade", help="migrate the catalog schema to this release's head revision"
    )
    _add_store_arguments(db_upgrade)
    db_upgrade.set_defaults(handler=_db_upgrade_command)
    db_reproject = db_commands.add_parser(
        "reproject",
        help=(
            "re-project incidents whose read models predate this release (a running "
            "server does this in the background)"
        ),
    )
    _add_store_arguments(db_reproject)
    db_reproject.add_argument(
        "--all",
        action="store_true",
        help=(
            "re-project every incident, verifying each artifact (after a restore, or to "
            "repair projections)"
        ),
    )
    db_reproject.set_defaults(handler=_db_reproject_command)
    db_transcript_index = db_commands.add_parser(
        "enable-transcript-index",
        help=(
            "Postgres: create the optional pg_trgm index that speeds up transcript search "
            "(q); outside the migration chain, idempotent"
        ),
    )
    _add_store_arguments(db_transcript_index)
    db_transcript_index.set_defaults(handler=_db_enable_transcript_index_command)

    prices = commands.add_parser("prices", help="manage the prices calls are costed with")
    prices_commands = prices.add_subparsers(dest="prices_command", required=True)
    prices_add = prices_commands.add_parser(
        "add", help="append a price (integer nano-USD per per-quantity units)"
    )
    _add_store_arguments(prices_add)
    scope = prices_add.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", help="the project the price is for")
    scope.add_argument(
        "--builtin", action="store_true", help="a built-in price, for every project (operator)"
    )
    prices_add.add_argument("--provider", required=True)
    prices_add.add_argument("--model", required=True)
    prices_add.add_argument(
        "--unit",
        required=True,
        help="input_token, output_token, cached_input_token, character or audio_second",
    )
    prices_add.add_argument("--price-nano-usd", type=int, required=True)
    prices_add.add_argument("--per-quantity", type=int, default=1)
    prices_add.add_argument(
        "--effective-from-unix-nano",
        type=int,
        default=0,
        help="calls whose first evidence is at or after this time use it (default 0: all)",
    )
    prices_add.add_argument("--source-url")
    prices_add.add_argument("--note")
    prices_add.set_defaults(handler=_prices_add_command)
    prices_list = prices_commands.add_parser(
        "list", help="list the built-in prices and a project's own"
    )
    _add_store_arguments(prices_list)
    prices_list.add_argument("--project")
    prices_list.set_defaults(handler=_prices_list_command)
    return parser


_REPORTED_ERRORS: tuple[type[Exception], ...] = (
    AssemblyError,
    ExportPolicyError,
    IncidentCodecError,
    JournalUnreadableError,
    OSError,
    ValueError,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except Exception as error:
        # Only commands that opened the store can raise a storage error, and they
        # imported storage to do so.
        storage = sys.modules.get(f"{__package__}.storage")
        storage_errors = () if storage is None else (storage.StorageError,)
        if not isinstance(error, _REPORTED_ERRORS + storage_errors):
            raise
        # CLI output should help an operator without reflecting incident payloads.
        # A store that refuses to open says why with a stable code and a message
        # that never carries evidence.
        server_config = sys.modules.get(f"{__package__}.server_config")
        configuration_error = server_config is not None and isinstance(
            error, server_config.ConfigurationError
        )
        if configuration_error or (
            storage is not None
            and isinstance(error, (storage.StorageOpenError, storage.CapturePresetUnavailableError))
        ):
            print(f"earshot: {error}", file=sys.stderr)
        else:
            print(f"earshot: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
