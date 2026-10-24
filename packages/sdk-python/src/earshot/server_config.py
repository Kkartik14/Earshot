"""One configuration parser for both server entry points.

``earshot serve`` and the ASGI module ``apps/ingest/app.py`` build their server
from the same ``ServerSettings``, read by ``load_server_settings`` and assembled
by ``open_server``. For ``serve``, a command-line flag overrides its variable;
otherwise a variable overrides the default. ``docs/configuration.md`` lists every
variable for each entry point, before and after this parser.

Rules that hold for every variable:

- an unset variable takes its default;
- a switch accepts ``1``/``true``/``yes`` or ``0``/``false``/``no``/empty, in any
  case, and refuses anything else instead of reading it as off;
- an invalid value refuses to start with ``ConfigurationError``, whose message
  names the variable and never repeats the value: database URLs, blob URLs and
  tokens can carry secrets.

Secrets are not settings: the correlation key (``EARSHOT_CORRELATION_KEY`` or
``_FILE``) is read by the store, the checkpoint key by
``checkpoint_key_from_environment``, and S3 credentials by the AWS SDK from its
standard variables.

This module imports only the standard library; ``open_server`` imports the
server runtime when it runs.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import parse_qsl, unquote, urlsplit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .maintenance import ServerLock

_T = TypeVar("_T")

DATABASE_URL_ENVIRONMENT = "EARSHOT_DATABASE_URL"
BLOB_URL_ENVIRONMENT = "EARSHOT_BLOB_URL"
DB_POOL_SIZE_ENVIRONMENT = "EARSHOT_DB_POOL_SIZE"
DEFAULT_DB_POOL_SIZE = 8
# One mutation can hold two statement connections at once (a purge inside a read),
# so a smaller pool would wait on itself.
MINIMUM_DB_POOL_SIZE = 2
_POSTGRES_SCHEMES = ("postgresql://", "postgres://", "postgresql+psycopg://")
_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no", ""})
# The longest call lifecycle duration (ten years). Added to today's Unix nanoseconds
# it stays far inside int64, which runs out in 2262.
MAX_CALL_TIMING_SECONDS = 10 * 365 * 24 * 3600
_S3_OPTIONS = frozenset({"endpoint", "region", "path_style"})


class ConfigurationError(ValueError):
    """A server setting is invalid. The message names it and never its value."""


@dataclass(frozen=True, slots=True)
class FileBlobLocation:
    """A ``file://`` blob store: its root directory."""

    root: Path


@dataclass(frozen=True, slots=True)
class S3BlobLocation:
    """An ``s3://`` blob store. Credentials come from the AWS SDK, never the URL."""

    bucket: str
    prefix: str
    endpoint: str | None
    region: str | None
    path_style: bool


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Every setting of a server process, from flags, the environment and defaults."""

    data_dir: Path
    database_url: str | None
    blob_url: str
    db_pool_size: int
    host: str
    port: int
    web_dir: str | None
    checkpoint_dir: str | None
    capture_journal_dir: str | None
    token: str | None
    auth_mode: str
    jwt_issuer: str | None
    jwt_audience: str | None
    jwks_url: str | None
    jwks_ca_file: str | None
    platform_adapter: bool
    behind_tls_proxy: bool
    trust_local_network: bool
    hosted_runtime_names: frozenset[str]
    hosted_session_statuses: frozenset[str]
    hosted_event_names: frozenset[str]
    max_body_bytes: int
    max_connector_body_bytes: int
    max_connector_deliveries_per_minute: int
    retention_cleanup_interval_seconds: float
    retention_cleanup_batch_size: int
    # The call lifecycle (``call_timings``), the OTLP receiver's bounds and the
    # recordings API's upload bound.
    call_end_grace_seconds: float
    call_long_quiet_seconds: float
    call_late_debounce_seconds: float
    recording_upload_hold_seconds: float
    otlp_max_protobuf_bytes: int
    otlp_max_json_bytes: int
    recording_max_bytes: int

    def call_timings(self) -> Any:
        """The ``storage_calls.CallTimings`` these settings describe."""

        from .storage_calls import CallTimings

        return CallTimings(
            end_grace_seconds=self.call_end_grace_seconds,
            long_quiet_seconds=self.call_long_quiet_seconds,
            late_debounce_seconds=self.call_late_debounce_seconds,
            upload_hold_seconds=self.recording_upload_hold_seconds,
        )

    def api_config(self) -> Any:
        """The ``ApiConfig`` these settings describe."""

        from .analysis import ANALYZER_VERSION
        from .api import ApiConfig

        try:
            return ApiConfig(
                host=self.host,
                token=self.token,
                auth_mode=self.auth_mode,  # type: ignore[arg-type]
                jwt_issuer=self.jwt_issuer,
                jwt_audience=self.jwt_audience,
                jwks_url=self.jwks_url,
                jwks_ca_file=self.jwks_ca_file,
                hosted_runtime_names=self.hosted_runtime_names,
                hosted_session_statuses=self.hosted_session_statuses,
                hosted_event_names=self.hosted_event_names,
                max_body_bytes=self.max_body_bytes,
                max_connector_body_bytes=self.max_connector_body_bytes,
                max_connector_deliveries_per_minute=self.max_connector_deliveries_per_minute,
                otlp_max_protobuf_bytes=self.otlp_max_protobuf_bytes,
                otlp_max_json_bytes=self.otlp_max_json_bytes,
                # One OTLP request in flight per pooled database connection.
                otlp_max_concurrent_requests=self.db_pool_size,
                recording_max_bytes=self.recording_max_bytes,
                retention_cleanup_interval_seconds=self.retention_cleanup_interval_seconds,
                retention_cleanup_batch_size=self.retention_cleanup_batch_size,
                analyzer_version=ANALYZER_VERSION,
                behind_tls_proxy=self.behind_tls_proxy,
                trust_local_network=self.trust_local_network,
            )
        except ValueError as error:
            # ApiConfig's messages are fixed text about combinations of settings.
            raise ConfigurationError(str(error)) from None


def load_server_settings(
    environ: Mapping[str, str] | None = None, **overrides: Any
) -> ServerSettings:
    """Read the server settings; ``overrides`` (flags) win over ``environ``.

    An override of ``None`` means the flag was not given.
    """

    environment = os.environ if environ is None else environ
    known = {field.name for field in fields(ServerSettings)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TypeError(f"unknown server settings: {', '.join(unknown)}")

    def setting(field: str, variable: str, parse: Callable[[str, str], _T], default: _T) -> _T:
        override = overrides.get(field)
        if override is not None:
            return override
        raw = environment.get(variable)
        return default if raw is None else parse(variable, raw)

    data_dir = setting("data_dir", "EARSHOT_DATA_DIR", lambda _, raw: Path(raw), Path(".earshot"))
    data_dir = Path(data_dir).expanduser()
    return ServerSettings(
        data_dir=data_dir,
        database_url=setting("database_url", DATABASE_URL_ENVIRONMENT, _database_url, None),
        blob_url=setting(
            "blob_url", BLOB_URL_ENVIRONMENT, _blob_url, (data_dir.resolve() / "blobs").as_uri()
        ),
        db_pool_size=setting(
            "db_pool_size", DB_POOL_SIZE_ENVIRONMENT, parse_pool_size, DEFAULT_DB_POOL_SIZE
        ),
        host=setting("host", "EARSHOT_HOST", _text, "127.0.0.1"),
        port=setting("port", "EARSHOT_PORT", _port, 4319),
        web_dir=setting("web_dir", "EARSHOT_WEB_DIR", _directory, None),
        checkpoint_dir=setting("checkpoint_dir", "EARSHOT_CHECKPOINT_DIR", _directory, None),
        capture_journal_dir=setting(
            "capture_journal_dir", "EARSHOT_CAPTURE_JOURNAL_DIR", _directory, None
        ),
        token=setting("token", "EARSHOT_TOKEN", _text, None),
        auth_mode=setting("auth_mode", "EARSHOT_AUTH_MODE", _auth_mode, "operator"),
        jwt_issuer=setting("jwt_issuer", "EARSHOT_JWT_ISSUER", _text, None),
        jwt_audience=setting("jwt_audience", "EARSHOT_JWT_AUDIENCE", _text, None),
        jwks_url=setting("jwks_url", "EARSHOT_JWKS_URL", _text, None),
        jwks_ca_file=setting("jwks_ca_file", "EARSHOT_JWKS_CA_FILE", _text, None),
        platform_adapter=setting(
            "platform_adapter", "EARSHOT_PLATFORM_ADAPTER_ENABLED", _switch, False
        ),
        behind_tls_proxy=setting("behind_tls_proxy", "EARSHOT_BEHIND_TLS_PROXY", _switch, False),
        trust_local_network=setting(
            "trust_local_network", "EARSHOT_TRUST_LOCAL_NETWORK", _switch, False
        ),
        hosted_runtime_names=setting(
            "hosted_runtime_names", "EARSHOT_HOSTED_RUNTIME_NAMES", _names, frozenset()
        ),
        hosted_session_statuses=setting(
            "hosted_session_statuses", "EARSHOT_HOSTED_SESSION_STATUSES", _names, frozenset()
        ),
        hosted_event_names=setting(
            "hosted_event_names", "EARSHOT_HOSTED_EVENT_NAMES", _names, frozenset()
        ),
        max_body_bytes=setting(
            "max_body_bytes", "EARSHOT_MAX_BODY_BYTES", _positive_integer, 16 * 1024 * 1024
        ),
        max_connector_body_bytes=setting(
            "max_connector_body_bytes",
            "EARSHOT_MAX_CONNECTOR_BODY_BYTES",
            _positive_integer,
            2 * 1024 * 1024,
        ),
        max_connector_deliveries_per_minute=setting(
            "max_connector_deliveries_per_minute",
            "EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE",
            _positive_integer,
            120,
        ),
        retention_cleanup_interval_seconds=setting(
            "retention_cleanup_interval_seconds",
            "EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS",
            _positive_seconds,
            5.0,
        ),
        retention_cleanup_batch_size=setting(
            "retention_cleanup_batch_size",
            "EARSHOT_RETENTION_CLEANUP_BATCH_SIZE",
            _positive_integer,
            1000,
        ),
        call_end_grace_seconds=setting(
            "call_end_grace_seconds", "EARSHOT_CALL_END_GRACE_SECONDS", _call_seconds, 10.0
        ),
        call_long_quiet_seconds=setting(
            "call_long_quiet_seconds",
            "EARSHOT_CALL_LONG_QUIET_SECONDS",
            _call_seconds,
            300.0,
        ),
        call_late_debounce_seconds=setting(
            "call_late_debounce_seconds",
            "EARSHOT_CALL_LATE_DEBOUNCE_SECONDS",
            _call_seconds,
            30.0,
        ),
        recording_upload_hold_seconds=setting(
            "recording_upload_hold_seconds",
            "EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS",
            _call_seconds,
            600.0,
        ),
        otlp_max_protobuf_bytes=setting(
            "otlp_max_protobuf_bytes",
            "EARSHOT_OTLP_MAX_PROTOBUF_BYTES",
            _positive_integer,
            32 * 1024 * 1024,
        ),
        otlp_max_json_bytes=setting(
            "otlp_max_json_bytes", "EARSHOT_OTLP_MAX_JSON_BYTES", _positive_integer, 4 * 1024 * 1024
        ),
        recording_max_bytes=setting(
            "recording_max_bytes",
            "EARSHOT_RECORDING_MAX_BYTES",
            _positive_integer,
            512 * 1024 * 1024,
        ),
    )


def open_server(
    settings: ServerSettings, *, auto_migrate: bool = True
) -> tuple[FastAPI, ServerLock | None]:
    """Open the store and build the app both entry points serve.

    The server lock is taken first (``K_server`` on Postgres, ``.server.lock`` in
    the data directory on SQLite), so a refused second server never migrates or
    writes. The app owns the lock afterwards (its lifespan releases it); on
    failure here it is released at once. The blob store (``EARSHOT_BLOB_URL``) is
    opened here too; the app sweeps it when it starts.
    """

    from .analysis import analyze_incident
    from .api import create_app
    from .blobstore import BlobStoreError, open_blob_store
    from .live import LiveSessionRegistry
    from .storage import IncidentStore, acquire_server_lock

    try:
        blob_store = open_blob_store(settings.blob_url)
    except BlobStoreError as error:
        # The one way to get here is an s3:// URL without boto3; the message names
        # the extra, never the URL.
        raise ConfigurationError(f"{BLOB_URL_ENVIRONMENT}: {error}") from None
    server_lock = acquire_server_lock(settings.database_url, settings.data_dir)
    try:
        store = IncidentStore(
            settings.data_dir, database_url=settings.database_url, auto_migrate=auto_migrate
        )
        try:
            config = settings.api_config()
            # Following a checkpoint directory is an explicit opt-in: it is the
            # same storage decision as writing one, seen from the other end.
            registry = (
                None
                if settings.checkpoint_dir is None
                else LiveSessionRegistry(
                    journal_dir=Path(settings.checkpoint_dir).expanduser().resolve(),
                    key=checkpoint_key_from_environment(),
                )
            )
            app = create_app(
                store=store,
                analyzer=analyze_incident,
                config=config,
                call_timings=settings.call_timings(),
                web_dir=settings.web_dir,
                live_registry=registry,
                capture_journal_dir=settings.capture_journal_dir,
                enable_platform_adapter=settings.platform_adapter,
                server_lock=server_lock,
                blob_store=blob_store,
            )
        except BaseException:
            store.close()
            raise
    except BaseException:
        if server_lock is not None:
            server_lock.release()
        raise
    return app, server_lock


def checkpoint_key_from_environment() -> bytes | None:
    """The checkpoint journal key: ``EARSHOT_CHECKPOINT_KEY`` or ``_FILE`` (or the
    spool key variables, for journals written by an older SDK)."""

    from .checkpoint.keys import resolve_at_rest_key

    return resolve_at_rest_key(
        None,
        env_var="EARSHOT_CHECKPOINT_KEY",
        env_file_var="EARSHOT_CHECKPOINT_KEY_FILE",
        label="checkpoint key",
        fallback=("EARSHOT_SPOOL_KEY", "EARSHOT_SPOOL_KEY_FILE"),
    )


def parse_pool_size(variable: str, raw: str) -> int:
    """``EARSHOT_DB_POOL_SIZE``: an integer of at least ``MINIMUM_DB_POOL_SIZE``."""

    if not raw.strip():
        return DEFAULT_DB_POOL_SIZE
    size = _integer(variable, raw)
    if size < MINIMUM_DB_POOL_SIZE:
        raise ConfigurationError(f"{variable} must be at least {MINIMUM_DB_POOL_SIZE}")
    return size


def _text(_variable: str, raw: str) -> str:
    return raw


def _directory(_variable: str, raw: str) -> str | None:
    # An empty value means "not configured", never "the current directory".
    return raw or None


def _switch(variable: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigurationError(f"{variable} must be one of 1, true, yes, 0, false, no")


def _integer(variable: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{variable} must be an integer") from None


def _positive_integer(variable: str, raw: str) -> int:
    value = _integer(variable, raw)
    if value < 1:
        raise ConfigurationError(f"{variable} must be a positive integer")
    return value


def _port(variable: str, raw: str) -> int:
    value = _integer(variable, raw)
    if not 1 <= value <= 65_535:
        raise ConfigurationError(f"{variable} must be a TCP port between 1 and 65535")
    return value


def _positive_seconds(variable: str, raw: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{variable} must be a number of seconds") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{variable} must be a positive, finite number of seconds")
    return value


def valid_call_timing(seconds: float) -> bool:
    """Whether ``seconds`` is a call lifecycle duration (``storage_calls.CallTimings``):
    at least one nanosecond and at most ``MAX_CALL_TIMING_SECONDS``. The closer adds
    these to nanosecond times kept as signed 64-bit integers, so an unbounded value
    would fail every request instead of the start."""

    return math.isfinite(seconds) and seconds * 1e9 >= 1 and seconds <= MAX_CALL_TIMING_SECONDS


CALL_TIMING_RANGE = f"between one nanosecond and {MAX_CALL_TIMING_SECONDS} seconds (ten years)"


def _call_seconds(variable: str, raw: str) -> float:
    """A call lifecycle duration (``EARSHOT_CALL_*``, the upload hold)."""

    value = _positive_seconds(variable, raw)
    if not valid_call_timing(value):
        raise ConfigurationError(f"{variable} must be {CALL_TIMING_RANGE}")
    return value


def _names(_variable: str, raw: str) -> frozenset[str]:
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _auth_mode(variable: str, raw: str) -> str:
    if raw not in {"operator", "hosted_jwt"}:
        raise ConfigurationError(f"{variable} must be 'operator' or 'hosted_jwt'")
    return raw


def _database_url(variable: str, raw: str) -> str | None:
    if not raw:
        return None
    if not raw.startswith(_POSTGRES_SCHEMES):
        raise ConfigurationError(
            f"{variable} must be a postgresql:// URL (leave it unset for SQLite in the "
            "data directory)"
        )
    return raw


def _blob_url(variable: str, raw: str) -> str:
    parse_blob_url(raw, variable)
    return raw


def parse_blob_url(
    raw: str, variable: str = BLOB_URL_ENVIRONMENT
) -> FileBlobLocation | S3BlobLocation:
    """``file:///absolute/path`` or ``s3://bucket[/prefix][?endpoint=&region=&path_style=]``.

    The one reading of a blob URL: settings validate with it and the blob store
    opens from it. A ``file://`` path is percent-decoded (RFC 8089), so a data
    directory holding ``#``, ``%`` or a space round-trips through
    ``Path.as_uri()``.
    """

    try:
        parts = urlsplit(raw)
        options = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        raise ConfigurationError(f"{variable} could not be parsed") from None
    if parts.fragment or raw.endswith("#"):
        raise ConfigurationError(f"{variable} must not have a #fragment")
    if parts.scheme == "file":
        if parts.netloc not in ("", "localhost") or not parts.path.startswith("/") or parts.query:
            raise ConfigurationError(f"{variable} must be file:///an/absolute/path")
        return FileBlobLocation(root=Path(unquote(parts.path)))
    if parts.scheme != "s3":
        raise ConfigurationError(f"{variable} must be a file:// or s3:// URL")
    if "@" in parts.netloc:
        raise ConfigurationError(
            f"{variable} must not carry credentials; S3 reads them from the standard AWS "
            "variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) or its credential chain"
        )
    if not parts.netloc:
        raise ConfigurationError(f"{variable} must name a bucket: s3://bucket/prefix")
    names = [name for name, _ in options]
    if len(names) != len(set(names)) or not set(names) <= _S3_OPTIONS:
        raise ConfigurationError(
            f"{variable} accepts each of endpoint, region and path_style at most once"
        )
    values = dict(options)
    if values.get("path_style", "0") not in {"0", "1"}:
        raise ConfigurationError(f"{variable} path_style must be 0 or 1")
    if values.get("region") == "":
        raise ConfigurationError(f"{variable} region must not be empty")
    endpoint = values.get("endpoint")
    if endpoint is not None:
        endpoint_parts = urlsplit(endpoint)
        if endpoint_parts.scheme not in {"http", "https"} or not endpoint_parts.netloc:
            raise ConfigurationError(f"{variable} endpoint must be an http(s) URL")
        if "@" in endpoint_parts.netloc:
            raise ConfigurationError(f"{variable} endpoint must not carry credentials")
    return S3BlobLocation(
        bucket=parts.netloc,
        prefix=unquote(parts.path).strip("/"),
        endpoint=endpoint,
        region=values.get("region"),
        path_style=values.get("path_style") == "1",
    )
