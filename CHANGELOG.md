# Changelog

Notable changes to Earshot. Versions follow the
[alpha compatibility policy](docs/release.md#alpha-compatibility-policy). Earlier releases
are described in their [GitHub release notes](https://github.com/Kkartik14/Earshot/releases).

## 0.2.0 (Unreleased)

Earshot now receives OpenTelemetry traces and call recordings, groups them into Calls, and
runs on SQLite or Postgres with local or S3 storage for audio. Read the Breaking section
before upgrading a 0.1.x installation.

### Breaking

- **Storage-backed CLI commands need the `server` extra.** On a base install,
  `earshot list`, `ingest`, `show`, `purge`, `project`, `api-key`, `connector`,
  `maintenance`, `db` and `recover --ingest` now exit with status 2 and a message naming
  `earshot-observability[server]`. Before, only `earshot serve` needed it. `validate`,
  `query`, `contradictions`, `diff`, `export` and `checkpoints` still work on a base
  install.
- **Upgrading a 0.1.x data directory is one-way.** The catalog moves to Alembic and its
  `PRAGMA user_version` becomes 1000, so every 0.1.x binary refuses it as newer than
  itself. Back up before upgrading; to go back, restore the backup. See
  [upgrading from 0.1.x](docs/storage-and-blobs.md#upgrading-from-01x).
- **Capture Presets change what the server stores.** A database created by 0.2.0 starts
  with every Project at `full`: the OTLP receiver keeps transcripts, model and tool
  payloads, the session key and recordings. An upgraded 0.1.x database sets every existing
  Project to `metadata_only`, and its Recording uploads are refused with 422 until an
  operator runs `earshot project set-capture --preset full`. See
  [privacy](docs/privacy.md#capture-presets).
- **Earshot now holds media bytes.** ADR 0004 supersedes the "Custody, not content" rule:
  Recordings for `full` Projects are stored in Earshot's blob store. `MediaRef`s naming
  other custodians keep their earlier meaning.
- **Switch variables are strict.** `EARSHOT_BEHIND_TLS_PROXY`,
  `EARSHOT_TRUST_LOCAL_NETWORK` and `EARSHOT_PLATFORM_ADAPTER_ENABLED` accept only `1`,
  `true`, `yes`, `0`, `false`, `no` or an empty value. Any other value refuses to start;
  before, it silently meant "off".
- **Postgres needs a correlation key.** A `postgresql://` database refuses to open without
  `EARSHOT_CORRELATION_KEY` or `EARSHOT_CORRELATION_KEY_FILE`
  (`EARSHOT_CORRELATION_KEY_REQUIRED`), and refuses a different key than the one it was
  created with (`EARSHOT_CORRELATION_KEY_MISMATCH`). To keep a SQLite store's identities,
  point the file variable at its `instance-correlation.key`.
- **The loopback `Host` check changed.** Under `EARSHOT_BEHIND_TLS_PROXY` (without
  `EARSHOT_TRUST_LOCAL_NETWORK`) it no longer applies, because every request there needs
  a credential: a TLS proxy in front of a `127.0.0.1` listener can now forward the public
  `Host`, where before every such request got 400 `EARSHOT_UNTRUSTED_HOST`. Under
  `EARSHOT_TRUST_LOCAL_NETWORK`, a `/v1` request with a valid API key or the
  `EARSHOT_TOKEN` operator token is now exempt from the check; anonymous requests and
  connector webhooks keep it. See the
  [host rule](docs/otlp-quickstart.md#api-keys-and-the-host-rule).
- **The hosted `/openapi.json` lists fewer routes.** In `hosted_jwt` mode it omits
  `/v1/traces`, `/v1/otlp/stats`, `/v1/calls*` and `/v1/recordings*`, which answer 403
  `EARSHOT_OPERATION_UNAVAILABLE` there for every method. A client generated from a
  hosted server's contract does not have them; `spec/backend-api.openapi.json` still
  lists them.
- **`last_used_at` can lag.** An API key's `last_used_at` write now waits at most 0.1 s
  for the store. When the store is busy the write is skipped and a later request makes it,
  so a busy store no longer delays an authenticated request on any route.
- **One server per SQLite data directory.** `earshot serve` and the ASGI module hold an
  exclusive lock on `<data-dir>/.server.lock` while they run. A second server, or a second
  uvicorn worker, on the same data directory exits with `EARSHOT_DATABASE_IN_USE` (status
  2, or 3 for the ASGI module), as on Postgres. Before, nothing stopped it, and the second
  server's cleanup could delete the first one's stored recordings. CLI commands do not
  take this lock and keep working while the server runs.
- **Spans containing U+0000 are rejected.** The OTLP receiver rejects a span whose name,
  instrumentation scope name or Session Key contains U+0000, on SQLite and Postgres
  alike, through `partial_success`; the rest of the request is stored. It also rejects a
  span with an attribute key longer than 1024 bytes (on the span, an event, a link, its
  resource or its scope), counted in `rejected_spans`.
- **OTLP requests have bounds on what their spans share.** A span is rejected through
  `partial_success`, and counted in `rejected_spans`, when its instrumentation scope name
  is longer than 256 bytes, its scope's attributes are over 16 KiB serialized, or its
  resource is over 64 KiB serialized (attributes and schema URL included). A request
  keeps the spans of its first 256 distinct instrumentation scopes and rejects those of
  any later one. Because a span's row holds its scope name, and a resource and scope
  are stored again with each distinct pair of them, a request's spans may store at
  most 8 MiB more than once; the spans past it are rejected. The rest of the request is
  stored. A 0.16 MB request could ask for 32 MiB before; ordinary exports use kilobytes
  of the budget. See
  [production notes](docs/production.md#exposure-and-limits).

### Added

- **OTLP/HTTP receiver** at `POST /v1/traces`: protobuf and JSON, `gzip` and `deflate`,
  per-span rejection through `partial_success`, idempotent re-sends (a changed payload
  keeps the first and counts `span_conflicts`), and 503 with `Retry-After: 3` when
  `EARSHOT_DB_POOL_SIZE` requests are in flight or the store's locks are not free within
  2 s, never 429. A request is one transaction with batched inserts, answered after it
  commits: in `scripts/bench_otlp.py` on one developer machine, a 512-span request takes
  about 14 ms (p50) on SQLite and 31 ms on Postgres 16. Errors from the route are
  `google.rpc.Status` bodies, including 410 when the Project began deletion during the
  request. A request's work is linear in its size: a resource or scope shared by many
  spans is checked, filtered and stored once per request. Known limit on Postgres: a
  request whose spans start thousands of new traces is slow (8,192 traces took 1.4 to
  2.5 s; SDK exporters send 512 spans per request by default). `GET /v1/otlp/stats`
  reports rare-event counters. The OTLP, recording and call
  routes answer 403 in `hosted_jwt` mode. See the
  [OTLP quick start](docs/otlp-quickstart.md).
- **Calls.** Spans are grouped by Session Key (`earshot.session.id`,
  `gen_ai.conversation.id`, `session.id`, `conversation.id`) and trace; an unkeyed trace
  is parked until a keyed span binds it, and after the quiet period becomes a Call keyed
  by its trace ID or is discarded. A Call closes after an end marker
  (`EARSHOT_CALL_END_GRACE_SECONDS`) or a quiet period
  (`EARSHOT_CALL_LONG_QUIET_SECONDS`), and each close stores a Revision, an ordinary
  Incident with bundle ID `<call_id>.r<n>`, built from the Call's Pipecat spans and stored
  Recordings. Late evidence produces the next Revision, no sooner than
  `EARSHOT_CALL_LATE_DEBOUNCE_SECONDS`, up to 16 Revisions plus a final end-signal one;
  past the cap the Call reports `evidence_after_latest_revision`. LLM input history is
  always left out of a Revision, with a declared omission. `/v1/metrics/turns` and Turn
  Fact queries count only a Call's latest Revision. Both entry points run the closer, and
  a crash between a claim and its commit is recovered when the server starts.
  Revisions are built in one child process per server, started before the server serves:
  a server whose child cannot start refuses to start (`EARSHOT_MATERIALIZER_START_FAILED`,
  exit status 3), a build that does not finish in 600 s (sending included) has its child
  killed and replaced, and at shutdown a build gets 10 s before its child is killed. The
  child's peak memory grows with the square of a Call's turns: about 90 MiB at 100 turns
  and 600 to 700 MiB at 1,000 (see [production notes](docs/production.md#exposure-and-limits)).
  Known limit on Postgres: closing a 600- to 1,000-turn Call delays one or two OTLP acks
  by 100 to 250 ms, never with a 503.
- **Recordings API**: `POST /v1/recordings`, `PUT /v1/recordings/{id}/content`,
  `GET /v1/recordings/{id}` and `GET /v1/recordings/{id}/content` with `Range`. Uploads
  stream to the blob store in constant memory, with an optional `Content-Digest`. A
  `pending` Recording holds its Call open until `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS`
  after the latest of its creation, the Call's latest evidence and its end marker, then
  fails. While a `PUT` of a Recording streams, its Call is not closed, so the first
  Revision includes the audio; a `PUT` that sends no body bytes for 60 s is ended with 408
  `EARSHOT_UPLOAD_IDLE_TIMEOUT`, and one still sending
  `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` after it started with 408
  `EARSHOT_UPLOAD_DEADLINE_EXCEEDED`; either stops holding the Call. A Call holds at most
  16 Recordings (a Revision's limit), and a 17th is refused with 409
  `EARSHOT_RECORDING_LIMIT`. A `Range` header longer than
  256 bytes is answered 416 unread. Mono only: a WAV with more than one channel is
  refused with 422 `EARSHOT_RECORDING_NOT_MONO`. A Session Key whose Call was purged is refused with 409
  `EARSHOT_CALL_PURGED` while the purge tombstone lives.
  `examples/pipecat_otlp/run_call.py --upload` uploads a real call's tracks.
- **Call API**: `GET /v1/calls` lists Calls newest first, with a cursor that stays stable
  while Calls arrive and receive evidence (`?state=`, `?limit=`, `?cursor=`).
  `GET /v1/calls/{call_id}` adds the Revisions, the Recordings and the transcript, read
  from the Call's spans so it grows while the Call is open. `POST /v1/calls:resolve` finds
  a Call by Session Key without echoing it. `DELETE /v1/calls/{call_id}` and
  `earshot call purge` purge a Call with its spans, Recordings, blobs and Revisions in one
  transaction. See the [OTLP quick start](docs/otlp-quickstart.md#check-the-result).
- **Call purge tombstones.** A purge tombstones the Session Key and each of the Call's
  traces, for `EARSHOT_CALL_LONG_QUIET_SECONDS` plus 24 hours. While a tombstone lives,
  new evidence for the key and late spans of those traces, keyed or not, are dropped and
  counted as `purged_call_evidence_dropped`, so an exporter's retries and Pipecat's late
  unkeyed spans cannot rebuild the Call. Afterwards the key starts a new Call with a new
  ID. Revisions carry no retention policy in 0.2.0, so no Call expires: a Call is kept
  until it is purged or its Project is deleted. See [privacy](docs/privacy.md#calls).
- **A Revision is purged only with its Call.** `DELETE /v1/incidents/{bundle_id}` on a
  Revision answers 409 `EARSHOT_INCIDENT_BELONGS_TO_CALL` with the `call_id`, and
  `earshot purge` exits 2 and names `earshot call purge`. Other Incidents purge as before.
- **Capture Presets** per Project (`full`, `metadata_only`; `redacted` is refused with
  `EARSHOT_PRESET_UNAVAILABLE`): `earshot project set-capture` and `earshot project show`.
- **Postgres catalog**: `EARSHOT_DATABASE_URL` and `--database-url` on every store command,
  `EARSHOT_DB_POOL_SIZE`, artifacts stored as rows, and one server per database
  (`EARSHOT_DATABASE_IN_USE`; the server exits with status 70 and
  `EARSHOT_DATABASE_LOCK_LOST` if it loses its lock). CLI writes are safe while the server
  runs. Tested on Postgres 16.
- **Blob store** for Recordings: `EARSHOT_BLOB_URL`, `file://` (default
  `<data-dir>/blobs`) or `s3://bucket/prefix?endpoint=…&region=…&path_style=1`, with
  credentials from the standard AWS variables.
- **Migrations**: Alembic revisions `0001_v19_baseline`, `0002_artifacts`,
  `0003_projection_version` and `0004_calls`; `earshot db current`, `earshot db upgrade`
  and `earshot db reproject [--all]`; `earshot serve --no-auto-migrate`. New refusal codes:
  `EARSHOT_SCHEMA_TOO_NEW`, `EARSHOT_SCHEMA_BEHIND`, `EARSHOT_CATALOG_INTEGRITY`,
  `EARSHOT_SERVER_EXTRA_REQUIRED`. `0004_calls` was amended before release: a catalog an
  unreleased 0.2.0 build stamped at `0004_calls` earlier lacks its last columns and is
  refused at open with `EARSHOT_CATALOG_INTEGRITY` ("catalog created by an unreleased
  0.2.0 build; recreate it"); recreate such development catalogs.
- **Extras**: `[server]` gains `sqlalchemy>=2.0,<3`, `alembic>=1.13,<2` and
  `opentelemetry-proto>=1.30,<2`. New `[postgres]` (`psycopg[binary]>=3.2,<4`) and `[s3]`
  (`boto3>=1.36,<2`). `[dev]` gains all of them. The base dependencies are unchanged.
- **New settings**, validated at start-up: `EARSHOT_BLOB_URL`, `EARSHOT_DB_POOL_SIZE`,
  `EARSHOT_CALL_END_GRACE_SECONDS`, `EARSHOT_CALL_LONG_QUIET_SECONDS`,
  `EARSHOT_CALL_LATE_DEBOUNCE_SECONDS`, `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS`,
  `EARSHOT_RECORDING_MAX_BYTES`, `EARSHOT_OTLP_MAX_PROTOBUF_BYTES` and
  `EARSHOT_OTLP_MAX_JSON_BYTES`. See [server configuration](docs/configuration.md).
- **`/readyz`** reports `projections_pending`.
- **New codes**: `EARSHOT_STORAGE_BUSY` (503 with `Retry-After: 3`), and the log codes
  `EARSHOT_SLOW_MUTATION`, `EARSHOT_PROJECTION_FAILED`, `EARSHOT_LEGACY_CAPTURE_PRESET`
  and `EARSHOT_WRITE_LOCK_LOST`.
- **Call, OTLP and Recording codes**:
  - problem codes `EARSHOT_CALL_NOT_FOUND`, `EARSHOT_CALL_PURGED`,
    `EARSHOT_RECORDING_NOT_FOUND`, `EARSHOT_RECORDING_CONFLICT`,
    `EARSHOT_RECORDING_NOT_STORED`, `EARSHOT_RECORDING_NOT_MONO`,
    `EARSHOT_RECORDING_INVALID_WAV`, `EARSHOT_RECORDING_CONTENT_MISSING`,
    `EARSHOT_PRESET_FORBIDS_MEDIA`, `EARSHOT_INVALID_SESSION_KEY`,
    `EARSHOT_CONTENT_DIGEST_MISMATCH`, `EARSHOT_INVALID_CONTENT_DIGEST`,
    `EARSHOT_CLIENT_DISCONNECTED`, `EARSHOT_UPLOAD_IDLE_TIMEOUT`,
    `EARSHOT_UPLOAD_DEADLINE_EXCEEDED`, `EARSHOT_RECORDING_LIMIT`,
    `EARSHOT_RANGE_NOT_SATISFIABLE`, `EARSHOT_BLOB_STORE_UNAVAILABLE` and
    `EARSHOT_INCIDENT_BELONGS_TO_CALL`;
  - a Call's `materialize_error` codes `EARSHOT_MATERIALIZE_INVALID_EVIDENCE`,
    `EARSHOT_MATERIALIZE_SNAPSHOT_GONE`, `EARSHOT_MATERIALIZE_CONFLICT`,
    `EARSHOT_MATERIALIZE_WORKER_FAILED`, `EARSHOT_MATERIALIZE_FINISH_FAILED` and
    `EARSHOT_CALL_INTEGRITY`;
  - the startup error `EARSHOT_MATERIALIZER_START_FAILED`: the server refuses to start
    (exit status 3, under both entry points) when the materializer's child process can't
    be started, and names the error's type and errno when the process could not be
    created at all;
  - log codes `EARSHOT_OTLP_SPANS_REJECTED`, `EARSHOT_BLOB_SWEEP`,
    `EARSHOT_BLOB_SWEEP_FAILED`, `EARSHOT_RECORDING_UPLOADS_FAILED`,
    `EARSHOT_RECORDING_DISCARD_DEFERRED`, `EARSHOT_BLOB_DELETE_RETRY`,
    `EARSHOT_MATERIALIZE_FAILED`, `EARSHOT_MATERIALIZER_STOPPED`, `EARSHOT_CALL_LEASE_LOST`,
    `EARSHOT_REVISION_STORED_DESPITE_ERROR` (a step after a Revision's commit raised; the
    closer reports the Revision, not a failure) and `EARSHOT_CALL_INTEGRITY`.
- **SDK**: `IncidentRecorder(manual_trace_id=)` fixes the trace ID of the recorder's
  manual operations, so two builds of the same evidence are byte-identical.
  `storage_calls.CallTimings` refuses a duration outside one nanosecond to ten years when
  it is built, as the server's settings do, so a host that builds its own `CallStore`
  cannot get a store whose every write overflows.
- **Semantic conventions**: `earshot.session.id` and `earshot.call.ended` are registered.
  This reverses the earlier note that reused `session.id` instead.
- **Packaging**: `@earshot/browser` and `@earshot/viewer-ui` are publish-ready at 0.2.0. The
  container image includes the `server`, `postgres` and `s3` extras and OCI labels. Before
  its first publish, `@earshot/viewer-ui`'s host-reported status view takes host-neutral
  names: the route `{ kind: "capture-status", hostCallId, capture }` and the
  `CaptureStatus` component. Hosts that linked the unpublished source must rename
  `call-status`, `callId` and `CallCaptureStatus`.
- **Examples and fixtures**: `examples/pipecat_otlp/` runs a real Pipecat 1.5 call over
  OTLP. Receiver tests build synthetic Pipecat-shaped requests in memory. The raw call
  fixture has been removed from the current tree, though its earlier commit remains in
  repository history. The upgrade fixtures in `fixtures/upgrade/` were written by four
  0.1.x releases. `run_call.py` fails
  when its call produced no `stt`, `llm` or `tts` span: Pipecat 1.5 only logs and retries
  when a provider refuses its key, so without the check such a call ends as if it had
  succeeded.
- **End-to-end tests.** A replay test
  (`apps/ingest/tests/e2e/test_otlp_replay_quickstart.py`) runs the README quick start on
  a real `earshot serve` with default settings: it replays generated OTLP requests and
  uploads synthetic recordings, and it runs in CI. A real-provider test
  (`apps/ingest/tests/e2e_real/test_pipecat_otlp_done_when.py`) runs one real Pipecat 1.5
  call (Deepgram, Groq, Cartesia) into Earshot on Postgres and S3. It skips without
  provider keys; `EARSHOT_REQUIRE_REAL_PROVIDERS=1` makes that a failure. Both require
  exactly one closed Call with a `final` end-signal Revision, its Recordings stored byte
  for byte, and every exported span stored.
- **Docs**: the [OTLP quick start](docs/otlp-quickstart.md),
  [storage and blobs](docs/storage-and-blobs.md), [production notes](docs/production.md),
  [server configuration](docs/configuration.md), and ADRs 0004 to 0007. ADR 0005
  supersedes ADR 0003.

### Changed

- **API version 0.14.0.** `API_VERSION` 0.13.0 adds the OTLP routes and
  `POST /v1/calls:resolve`, and 0.14.0 the Recordings routes; the call list, detail and
  purge routes ship under 0.14.0. Regenerate clients from `spec/backend-api.openapi.json`.
- **Project deletion covers every Call.** `DELETE /v1/projects/{project_id}` also removes
  the Project's Calls with all their evidence: spans, parked traces, Recordings and their
  blobs, Revisions, resources, key generations, Call tombstones and OTLP counters. It
  answers 202 until the blobs are deleted. See
  [project deletion](docs/backend-api.md#project-deletion).
- **One configuration parser** feeds `earshot serve` and `uvicorn apps.ingest.app:app`, so
  the same environment gives the same server.
- **ASGI app** (`uvicorn apps.ingest.app:app`):
  - `EARSHOT_TRUST_LOCAL_NETWORK` is honoured. Before, it was ignored, and a non-loopback
    `EARSHOT_HOST` refused to start.
  - `EARSHOT_CHECKPOINT_DIR` is honoured, with `EARSHOT_CHECKPOINT_KEY[_FILE]` (fallback
    `EARSHOT_SPOOL_KEY[_FILE]`), so the live tail under `/v1/live` works.
  - An invalid value refuses to start with `earshot: <VARIABLE> must …` and exit status 3,
    uvicorn's start-up-failure status. Before, it raised a traceback that echoed the value
    and exited 1. An invalid `EARSHOT_AUTH_MODE` is reported the same way.
  - A non-Postgres `EARSHOT_DATABASE_URL` is refused by the parser.
  - An empty `EARSHOT_CAPTURE_JOURNAL_DIR` means unset.
  - Unchanged: `EARSHOT_PORT` is not read (uvicorn's `--port` decides), `EARSHOT_HOST`
    describes uvicorn's listener, and `EARSHOT_WEB_DIR` is read.
- **`earshot serve`**:
  - `EARSHOT_CAPTURE_JOURNAL_DIR`, `EARSHOT_HOSTED_RUNTIME_NAMES`,
    `EARSHOT_HOSTED_SESSION_STATUSES` and `EARSHOT_HOSTED_EVENT_NAMES` are honoured. Before,
    they were ignored and hosted runtime events stayed closed.
  - A refusal names the variable, never its value, and exits with status 2.
  - An empty `EARSHOT_WEB_DIR` means unset.
- **Start-up no longer re-projects or verifies every artifact.** Stale projections are
  rebuilt in the background in bounded batches; `/readyz` reports how many remain. An
  unreadable artifact no longer stops the store from opening: it stays pending, is logged
  with `EARSHOT_PROJECTION_FAILED`, and is named by `earshot db reproject`. After a
  restore, run `earshot db reproject --all` to verify every artifact.
- `earshot list` re-projects stale Incidents for up to 10 s before it filters.
- SQLite compaction after a retention drain runs at most once every 10 minutes. An explicit
  purge still compacts at once.
- API-key checks: a positive scrypt verdict is cached for 60 s, keyed by a hash of the
  credential. Every request still checks that the key is active, so a revocation takes
  effect on the next request. `last_used_at` is written at most once a minute per key.
- Ingest computes projections before it takes the write lock, so writes hold the lock for
  less time.
- CI runs the storage modules on Postgres and the blob store on MinIO, and enforces branch
  coverage on the combined results.
- Removed the superseded M0 TypeScript prototype, `packages/schema` and `packages/analysis`.
  Neither was published.

### Fixed

- **Incidents written by 0.1.0 are readable again.** Builds of `main` after the 0.1.0 tag
  refused them as "canonical profile JSON is not RFC 8785/JCS canonical", and could not
  open a 0.1.0 data directory at all. 0.2.0 reads both stored profile forms, and the
  digests of everything written since are unchanged.
- An invalid numeric `EARSHOT_*` variable no longer breaks every `earshot` command; only
  `earshot serve` reads them.
- An empty `EARSHOT_CHECKPOINT_DIR` no longer resolves to the current directory.
- The server-side Pipecat adapter no longer crashes on real hex link IDs; it takes hex or
  integer IDs.
