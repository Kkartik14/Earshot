# Server configuration

Earshot has two ways to run its server:

- `earshot serve`, the command-line entry point;
- `uvicorn apps.ingest.app:app`, the ASGI module, for deployments that run uvicorn
  (or another ASGI server) themselves.

Both read their configuration through one parser, `earshot.server_config`, and
build the app through the same function (`open_server`). The same environment
therefore gives the same server under either entry point. For `earshot serve`, a
command-line flag overrides its variable; otherwise a variable overrides the
default.

## Rules for every variable

- **Unset** means the default.
- **Switches** accept `1`, `true` or `yes`, and `0`, `false`, `no` or an empty value,
  in any case. Any other value refuses to start (before 0.2.0 it silently meant
  "off").
- **Numbers** must be integers or finite numbers in range. An invalid value refuses
  to start.
- **A refusal names the variable and never repeats its value**, because database
  URLs, blob URLs and tokens can carry secrets. `earshot serve` exits with status 2;
  the ASGI module exits with status 3, uvicorn's start-up-failure status, so a
  multi-worker supervisor stops instead of restarting the worker.
- **An empty directory variable** (`EARSHOT_WEB_DIR`, `EARSHOT_CHECKPOINT_DIR`,
  `EARSHOT_CAPTURE_JOURNAL_DIR`) means "not configured", never the current
  directory.
- **No log line carries a secret.** The database URL is printed with its password
  masked (`postgresql://user:***@host/db`), and S3 credentials are never read by
  Earshot itself.

## Variables, by entry point

In the entry-point columns, _read_ means the entry point uses the variable, and
_ignored_ means it does not. **Bold** marks a change in 0.2.0. Variables new in
0.2.0 have no "before" value.

### Storage

| Variable                                                    | Default                             | `earshot serve` before → after | ASGI app before → after       | Notes                                                                                                                                                                                                                                                                                                                                                     |
| ----------------------------------------------------------- | ----------------------------------- | ------------------------------ | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EARSHOT_DATA_DIR` (`--data-dir`)                           | `.earshot`                          | read → read                    | read → read                   | SQLite catalog, local CAS objects, and the instance correlation key.                                                                                                                                                                                                                                                                                      |
| `EARSHOT_DATABASE_URL` (`--database-url`)                   | unset: SQLite in the data directory | read → read                    | read → read                   | `postgresql://…` selects Postgres. Any other scheme is refused.                                                                                                                                                                                                                                                                                           |
| `EARSHOT_DB_POOL_SIZE`                                      | `8` (at least `2`)                  | read by the store → read       | read by the store → read      | The Postgres connection pool size. On both databases it also caps the OTLP requests in flight; past it, `503` with `Retry-After: 3`. The parser and the store share one rule.                                                                                                                                                                             |
| `EARSHOT_CORRELATION_KEY` or `EARSHOT_CORRELATION_KEY_FILE` | none                                | read by the store → unchanged  | read by the store → unchanged | Required on Postgres; a secret. See [storage](./storage.md).                                                                                                                                                                                                                                                                                              |
| `EARSHOT_BLOB_URL`                                          | `file://<data-dir>/blobs`           | new: **read and validated**    | new: **read and validated**   | `file:///absolute/path`, percent-encoded as `Path.as_uri()` writes it (the default is built that way), or `s3://bucket/prefix?endpoint=…&region=…&path_style=0\|1` (the `s3` extra). A `#fragment` and credentials in the URL are refused; S3 uses the standard AWS credential variables. Used by the blob store (`earshot/blobstore.py`) for recordings. |

### Listener and access

| Variable                                                                                        | Default                              | `earshot serve` before → after           | ASGI app before → after        | Notes                                                                                                                                       |
| ----------------------------------------------------------------------------------------------- | ------------------------------------ | ---------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `EARSHOT_HOST` (`--host`)                                                                       | `127.0.0.1`                          | read → read                              | read → read                    | For the ASGI app, uvicorn's `--host` decides where it binds; this variable describes that listener to Earshot's access rules.               |
| `EARSHOT_PORT` (`--port`)                                                                       | `4319`                               | read → read, **validated as a TCP port** | ignored → ignored              | The ASGI app's port is uvicorn's `--port`.                                                                                                  |
| `EARSHOT_TOKEN` (`--token`)                                                                     | unset                                | read → read                              | read → read                    | Legacy operator bearer token; a secret. An empty value still requires authentication and matches no request.                                |
| `EARSHOT_AUTH_MODE` (`--auth-mode`)                                                             | `operator`                           | read → read                              | read → read                    | `operator` or `hosted_jwt`. Any other value is refused by name.                                                                             |
| `EARSHOT_JWT_ISSUER`, `EARSHOT_JWT_AUDIENCE`, `EARSHOT_JWKS_URL`, `EARSHOT_JWKS_CA_FILE`        | unset                                | read → read                              | read → read                    | `hosted_jwt` only.                                                                                                                          |
| `EARSHOT_BEHIND_TLS_PROXY` (`--behind-tls-proxy`)                                               | off                                  | read → read, **strict switch**           | read → read, **strict switch** |                                                                                                                                             |
| `EARSHOT_TRUST_LOCAL_NETWORK` (`--trust-local-network`)                                         | off                                  | read → read, **strict switch**           | **ignored → read**             | The container image (`compose.yaml`) relies on it. Before 0.2.0, the ASGI app refused a non-loopback `EARSHOT_HOST` even when this was set. |
| `EARSHOT_PLATFORM_ADAPTER_ENABLED` (`--platform-adapter`)                                       | off                                  | read → read, **strict switch**           | read → read, **strict switch** | `hosted_jwt` only.                                                                                                                          |
| `EARSHOT_HOSTED_RUNTIME_NAMES`, `EARSHOT_HOSTED_SESSION_STATUSES`, `EARSHOT_HOSTED_EVENT_NAMES` | empty (hosted runtime events closed) | **ignored → read**                       | read → read                    | Comma-separated allowlists. See [backend API](./backend-api.md).                                                                            |

### Viewer, live tail and browser capture

| Variable                                                                                            | Default                     | `earshot serve` before → after               | ASGI app before → after                        | Notes                                                                                                   |
| --------------------------------------------------------------------------------------------------- | --------------------------- | -------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `EARSHOT_WEB_DIR` (`--web-dir`)                                                                     | the packaged viewer, if any | read → read                                  | read by `create_app` → read                    | Serves the viewer at `/`. `create_app` keeps its own fallback to this variable for hosts that embed it. |
| `EARSHOT_CHECKPOINT_DIR` (`--checkpoint-dir`)                                                       | unset (no live tail)        | read → read                                  | **ignored → read**                             | Follows crash-recovery journals under `/v1/live`.                                                       |
| `EARSHOT_CHECKPOINT_KEY` or `EARSHOT_CHECKPOINT_KEY_FILE` (fallback `EARSHOT_SPOOL_KEY` or `_FILE`) | none                        | read with a checkpoint directory → unchanged | **ignored → read with a checkpoint directory** | Journal decryption key; a secret.                                                                       |
| `EARSHOT_CAPTURE_JOURNAL_DIR`                                                                       | unset (in memory)           | **ignored → read**                           | read → read                                    | Durable journals for continuous browser calls. Hosted capture v2 needs it.                              |

### Limits and maintenance

| Variable                                      | Default    | `earshot serve` before → after | ASGI app before → after | Notes             |
| --------------------------------------------- | ---------- | ------------------------------ | ----------------------- | ----------------- |
| `EARSHOT_MAX_BODY_BYTES` (`--max-body-bytes`) | `16777216` | read → read                    | read → read             | Positive integer. |
| `EARSHOT_MAX_CONNECTOR_BODY_BYTES`            | `2097152`  | read → read                    | read → read             | Positive integer. |
| `EARSHOT_MAX_CONNECTOR_DELIVERIES_PER_MINUTE` | `120`      | read → read                    | read → read             | Positive integer. |
| `EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS`  | `5`        | read → read                    | read → read             | Positive, finite. |
| `EARSHOT_RETENTION_CLEANUP_BATCH_SIZE`        | `1000`     | read → read                    | read → read             | 1 to 10000.       |

Before 0.2.0, `earshot serve` converted these defaults when it built its command
line, so an invalid value broke every `earshot` command, `earshot list` included.
Now only the server parses them.

### Calls, OTLP and recordings (new in 0.2.0)

These are parsed and validated at start-up. The four durations are seconds, decimals
allowed, from one nanosecond to ten years (`315360000`); a value outside that range
refuses to start with a message naming the variable. The
[OTLP quick start](./otlp-quickstart.md) describes the rules they set.

| Variable                                | Default     | Both entry points | Meaning                                                                                                                                                                                                    |
| --------------------------------------- | ----------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EARSHOT_CALL_END_GRACE_SECONDS`        | `10`        | new: read         | Time after an end marker before a call closes.                                                                                                                                                             |
| `EARSHOT_CALL_LONG_QUIET_SECONDS`       | `300`       | new: read         | Quiet time before a call without an end marker closes.                                                                                                                                                     |
| `EARSHOT_CALL_LATE_DEBOUNCE_SECONDS`    | `30`        | new: read         | Minimum wait before a closed call that received late evidence is revised.                                                                                                                                  |
| `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` | `600`       | new: read         | How long a pending recording holds its call open, counted from the latest of its creation, the call's latest evidence and the call's end marker. Then it fails, unless an upload of it is still streaming. |
| `EARSHOT_OTLP_MAX_PROTOBUF_BYTES`       | `33554432`  | new: read         | Decompressed OTLP/protobuf request limit.                                                                                                                                                                  |
| `EARSHOT_OTLP_MAX_JSON_BYTES`           | `4194304`   | new: read         | Decompressed OTLP/JSON request limit.                                                                                                                                                                      |
| `EARSHOT_RECORDING_MAX_BYTES`           | `536870912` | new: read         | Recording upload limit.                                                                                                                                                                                    |

## What is not a setting

These are constants, not knobs:

| Value                                                  | Constant                                   |
| ------------------------------------------------------ | ------------------------------------------ |
| Maintenance tick (closer, lock watchdog, reprojection) | 1 s                                        |
| OTLP lock budget (wait for every write lock)           | 2 s; past it, `503` with `Retry-After: 3`  |
| Write-lock hold that logs `EARSHOT_SLOW_MUTATION`      | 1 s                                        |
| API-key verdict cache                                  | 60 s, 1024 entries                         |
| `api_keys.last_used_at` write interval, per key        | 60 s                                       |
| SQLite compaction after a retention drain              | at most once every 10 min                  |
| Background reprojection batch                          | 100 incidents                              |
| Recording upload idle timeout (no body bytes)          | 60 s; past it, `408`                       |
| Recording upload deadline (one whole `PUT`)            | the upload hold, from its start; `408`     |
| Recordings per Call                                    | 16; the 17th is `409`                      |
| `Range` header length                                  | 256 bytes; past it, `416`                  |
| OTLP attribute key length                              | 1024 bytes; past it, the span is rejected  |
| OTLP instrumentation scope name                        | 256 bytes; past it, its spans are rejected |
| OTLP scope attributes, serialized                      | 16 KiB; past it, its spans are rejected    |
| OTLP resource, serialized, with its schema URL         | 64 KiB; past it, its spans are rejected    |
| OTLP distinct instrumentation scopes per request       | 256; later scopes' spans are rejected      |
| OTLP bytes one request stores more than once           | 8 MiB; spans past it are rejected          |
| Revision build deadline (materializer child)           | 600 s; past it, the child is killed        |
| Revision build grace at shutdown                       | 10 s; past it, the child is killed         |

## Other commands

Every other `earshot` command that opens the store reads `--data-dir`
(`EARSHOT_DATA_DIR`) and `--database-url` (`EARSHOT_DATABASE_URL`), and on Postgres
`EARSHOT_DB_POOL_SIZE` and the correlation key, with the same rules.

Connector webhook secrets are named by each connector's `env:NAME` reference and
are read when a delivery arrives. Test-only variables (`EARSHOT_TEST_MODE`,
`EARSHOT_TEST_CRASH_AT`, `EARSHOT_TEST_STALL_AT`, `EARSHOT_TEST_FAULT_AT`) do nothing in
production.
