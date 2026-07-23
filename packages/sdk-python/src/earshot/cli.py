"""Command-line entry point for local validation, storage, and serving."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .analysis import ANALYZER_VERSION, analyze_incident
from .codec import (
    IncidentCodecError,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from .exporters import span_count, to_openinference, to_otlp
from .exporters.push import OtlpHttpExporter
from .privacy import ExportPolicyError, assert_export_allowed
from .query import EvidenceQuery, compare_incidents
from .storage import DEFAULT_PROJECT_ID, IncidentStore, StorageError
from .validation import validate_incident


def _data_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("EARSHOT_DATA_DIR", ".earshot"))


def _boolean_environment(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return value.lower() in {"1", "true", "yes"} if value is not None else default


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
    store = IncidentStore(_data_dir(arguments.data_dir))
    result = store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id=arguments.project,
    )
    value = result.record.as_dict()
    value["created"] = result.created
    _print_json(value)
    return 0


def _list_command(arguments: argparse.Namespace) -> int:
    store = IncidentStore(_data_dir(arguments.data_dir))
    page = store.list_incidents(
        project_id=arguments.project,
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
    store = IncidentStore(_data_dir(arguments.data_dir))
    _, payload = store.get_artifact(arguments.bundle_id, project_id=arguments.project)
    bundle = decode_incident_protobuf(payload)
    assert_export_allowed(bundle, "local_cli")
    if arguments.format == "protobuf":
        sys.stdout.buffer.write(payload)
        return 0
    sys.stdout.buffer.write(encode_incident_json(bundle, indent=2))
    sys.stdout.buffer.write(b"\n")
    return 0


def _purge_command(arguments: argparse.Namespace) -> int:
    store = IncidentStore(_data_dir(arguments.data_dir))
    store.purge(arguments.bundle_id, project_id=arguments.project)
    _print_json({"bundle_id": arguments.bundle_id, "purged": True})
    return 0


def _project_create_command(arguments: argparse.Namespace) -> int:
    store = IncidentStore(_data_dir(arguments.data_dir))
    project = store.create_project(arguments.project_id, display_name=arguments.display_name)
    _print_json(
        {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "created_at_unix_nano": project.created_at_unix_nano,
        }
    )
    return 0


def _api_key_issue_command(arguments: argparse.Namespace) -> int:
    store = IncidentStore(_data_dir(arguments.data_dir))
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
    store = IncidentStore(_data_dir(arguments.data_dir))
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
    store = IncidentStore(_data_dir(arguments.data_dir))
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
    # Respect any declared export restriction, exactly like ``show`` does.
    assert_export_allowed(bundle, "otlp")
    document = to_openinference(bundle) if arguments.format == "openinference" else to_otlp(bundle)

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


def _serve_command(arguments: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .api import ApiConfig, create_app
    except ModuleNotFoundError:
        print(
            "earshot: server dependencies are not installed; "
            "install 'earshot-observability[server]'",
            file=sys.stderr,
        )
        return 2

    token = arguments.token or os.environ.get("EARSHOT_TOKEN")
    data_dir = _data_dir(arguments.data_dir).expanduser().resolve()
    config = ApiConfig(
        host=arguments.host,
        token=token,
        max_body_bytes=arguments.max_body_bytes,
        max_connector_body_bytes=arguments.max_connector_body_bytes,
        max_connector_deliveries_per_minute=(arguments.max_connector_deliveries_per_minute),
        analyzer_version=ANALYZER_VERSION,
        behind_tls_proxy=arguments.behind_tls_proxy,
        trust_local_network=arguments.trust_local_network,
    )
    app = create_app(
        data_dir=data_dir,
        analyzer=analyze_incident,
        config=config,
        web_dir=arguments.web_dir,
    )
    print(f"Earshot data path: {data_dir}", file=sys.stderr)
    print(f"Earshot listening: http://{arguments.host}:{arguments.port}/", file=sys.stderr)
    if arguments.trust_local_network:
        print(
            "Earshot warning: API authentication is disabled "
            "(--trust-local-network); keep this listener on a trusted boundary "
            "such as `docker -p 127.0.0.1:PORT`, never a public interface.",
            file=sys.stderr,
        )
    uvicorn.run(
        app,
        host=arguments.host,
        port=arguments.port,
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
    ingest.add_argument("--data-dir")
    ingest.add_argument("--project", default=DEFAULT_PROJECT_ID)
    ingest.set_defaults(handler=_ingest_command)

    list_parser = commands.add_parser("list", help="list locally stored incidents")
    list_parser.add_argument("--data-dir")
    list_parser.add_argument("--project", default=DEFAULT_PROJECT_ID)
    list_parser.add_argument("--session-id")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--cursor")
    list_parser.set_defaults(handler=_list_command)

    show = commands.add_parser("show", help="write one stored incident to stdout")
    show.add_argument("bundle_id")
    show.add_argument("--data-dir")
    show.add_argument("--project", default=DEFAULT_PROJECT_ID)
    show.add_argument("--format", choices=("json", "protobuf"), default="json")
    show.set_defaults(handler=_show_command)

    purge = commands.add_parser("purge", help="physically purge an incident")
    purge.add_argument("bundle_id")
    purge.add_argument("--data-dir")
    purge.add_argument("--project", default=DEFAULT_PROJECT_ID)
    purge.set_defaults(handler=_purge_command)

    project = commands.add_parser("project", help="manage project authorization scopes")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_create = project_commands.add_parser("create", help="create a project")
    project_create.add_argument("project_id")
    project_create.add_argument("--display-name", required=True)
    project_create.add_argument("--data-dir")
    project_create.set_defaults(handler=_project_create_command)

    api_key = commands.add_parser("api-key", help="manage project API keys")
    api_key_commands = api_key.add_subparsers(dest="api_key_command", required=True)
    api_key_issue = api_key_commands.add_parser("issue", help="issue a project API key")
    api_key_issue.add_argument("--project", required=True)
    api_key_issue.add_argument("--label", required=True)
    api_key_issue.add_argument("--data-dir")
    api_key_issue.set_defaults(handler=_api_key_issue_command)
    api_key_revoke = api_key_commands.add_parser("revoke", help="revoke a project API key")
    api_key_revoke.add_argument("--project", required=True)
    api_key_revoke.add_argument("--key-id", required=True)
    api_key_revoke.add_argument("--data-dir")
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
    connector_create.add_argument("--data-dir")
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
        help="project an incident into OTLP or OpenInference and optionally push it",
    )
    export.add_argument("path")
    export.add_argument("--format", choices=("otlp", "openinference"), required=True)
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

    serve = commands.add_parser("serve", help="run the local ingest API")
    serve.add_argument("--data-dir")
    serve.add_argument("--host", default=os.environ.get("EARSHOT_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("EARSHOT_PORT", "4319")))
    serve.add_argument(
        "--web-dir",
        default=os.environ.get("EARSHOT_WEB_DIR"),
        help="serve a built viewer SPA from this directory at the site root",
    )
    serve.add_argument("--token")
    serve.add_argument(
        "--behind-tls-proxy",
        action="store_true",
        default=_boolean_environment("EARSHOT_BEHIND_TLS_PROXY"),
        help="confirm that a same-host proxy exposes the loopback listener over HTTPS",
    )
    serve.add_argument(
        "--trust-local-network",
        action="store_true",
        default=_boolean_environment("EARSHOT_TRUST_LOCAL_NETWORK"),
        help=(
            "serve the API without authentication on a listener confined to a "
            "trusted local boundary (e.g. `docker -p 127.0.0.1:PORT`); never on "
            "a public interface"
        ),
    )
    serve.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(os.environ.get("EARSHOT_MAX_BODY_BYTES", str(16 * 1024 * 1024))),
    )
    serve.add_argument(
        "--max-connector-body-bytes",
        type=int,
        default=int(os.environ.get("EARSHOT_MAX_CONNECTOR_BODY_BYTES", str(2 * 1024 * 1024))),
    )
    serve.add_argument(
        "--max-connector-deliveries-per-minute",
        type=int,
        default=int(os.environ.get("EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE", "120")),
    )
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(handler=_serve_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        ExportPolicyError,
        IncidentCodecError,
        StorageError,
        OSError,
        ValueError,
    ) as error:
        # CLI output should help an operator without reflecting incident payloads.
        print(f"earshot: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
