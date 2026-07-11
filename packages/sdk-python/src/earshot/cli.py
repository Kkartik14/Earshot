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
from .api import ApiConfig, create_app
from .codec import (
    IncidentCodecError,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from .privacy import ExportPolicyError, assert_export_allowed
from .storage import IncidentStore, StorageError
from .validation import validate_incident


def _data_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("EARSHOT_DATA_DIR", ".earshot"))


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
    result = store.ingest(bundle, encode_incident_protobuf(bundle))
    value = result.record.as_dict()
    value["created"] = result.created
    _print_json(value)
    return 0


def _list_command(arguments: argparse.Namespace) -> int:
    store = IncidentStore(_data_dir(arguments.data_dir))
    page = store.list_incidents(
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
    _, payload = store.get_artifact(arguments.bundle_id)
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
    store.purge(arguments.bundle_id)
    _print_json({"bundle_id": arguments.bundle_id, "purged": True})
    return 0


def _serve_command(arguments: argparse.Namespace) -> int:
    import uvicorn

    token = arguments.token or os.environ.get("EARSHOT_TOKEN")
    config = ApiConfig(
        host=arguments.host,
        token=token,
        max_body_bytes=arguments.max_body_bytes,
        analyzer_version=ANALYZER_VERSION,
        behind_tls_proxy=arguments.behind_tls_proxy,
    )
    app = create_app(
        data_dir=_data_dir(arguments.data_dir),
        analyzer=analyze_incident,
        config=config,
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
    ingest.set_defaults(handler=_ingest_command)

    list_parser = commands.add_parser("list", help="list locally stored incidents")
    list_parser.add_argument("--data-dir")
    list_parser.add_argument("--session-id")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--cursor")
    list_parser.set_defaults(handler=_list_command)

    show = commands.add_parser("show", help="write one stored incident to stdout")
    show.add_argument("bundle_id")
    show.add_argument("--data-dir")
    show.add_argument("--format", choices=("json", "protobuf"), default="json")
    show.set_defaults(handler=_show_command)

    purge = commands.add_parser("purge", help="physically purge an incident")
    purge.add_argument("bundle_id")
    purge.add_argument("--data-dir")
    purge.set_defaults(handler=_purge_command)

    serve = commands.add_parser("serve", help="run the local ingest API")
    serve.add_argument("--data-dir")
    serve.add_argument("--host", default=os.environ.get("EARSHOT_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("EARSHOT_PORT", "4319")))
    serve.add_argument("--token")
    serve.add_argument(
        "--behind-tls-proxy",
        action="store_true",
        help="confirm that a same-host proxy exposes the loopback listener over HTTPS",
    )
    serve.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(os.environ.get("EARSHOT_MAX_BODY_BYTES", str(16 * 1024 * 1024))),
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
