# Changelog

Notable changes to Earshot. Versions follow the
[alpha compatibility policy](docs/release.md#alpha-compatibility-policy). Earlier releases
are described in their [GitHub release notes](https://github.com/Kkartik14/Earshot/releases).

## 0.3.0 (Unreleased)

Every closed Call now has a call view: its audio with a waveform, the transcript on the
audio's time axis, per-turn latency as the framework reports it beside the audible gap at
the agent measured from the recording, signals across the call, and its cost. The calls
list filters on all of it and searches transcript text. LiveKit Agents 1.6 to 1.8 calls
over OTLP get the same view as Pipecat 1.5. 0.2.0 was not published; the Breaking section
matters only to installs from source. See the
[0.3.0 release notes](docs/release-notes-0.3.0.md) for what the tests prove and don't.

### Breaking

- **A Recording with the wrong channel count gets `EARSHOT_RECORDING_CHANNEL_MISMATCH`**,
  which replaces `EARSHOT_RECORDING_NOT_MONO`.
- **Ogg uploads must be Ogg Opus.** Another Ogg codec gets 422
  `EARSHOT_RECORDING_INVALID_OGG`.
- **`metadata_only` drops LiveKit keys it does not list.** One table now classifies every
  `lk.*` key of LiveKit 1.6 to 1.8; an unlisted `lk.*` key (for example `lk.sip.*`) is
  dropped under `metadata_only`, where 0.2.0 kept it as metadata.
- **In-process LiveKit bundles record content under semantic keys**, with the content
  class instead of metadata: `lk.pii.user_transcript` and `lk.pii.input_text` become
  `speech.text`; `lk.pii.chat_ctx` becomes `gen_ai.input`; `lk.pii.response.text` becomes
  `gen_ai.output.messages`; `lk.pii.response.function_calls` becomes
  `gen_ai.output.messages.function_calls`; `lk.(pii.)user_input` becomes
  `gen_ai.input.user_message` (was `speech.text`); `lk.pii.instructions` becomes
  `gen_ai.input.system_instruction`; `lk.pii.participant_identity` becomes
  `participant.name`; and `lk.room_name` becomes `lk.pii.room_name`.

### Added

- **TVIC runtime adapter.** An in-process, metadata-only receiver for TVIC's bounded
  runtime-observation envelope, mapped into normalized events and coverage. See
  [the TVIC adapter](docs/adapters/tvic.md).
- **The call view.** `GET /v1/calls/{id}` keeps every 0.2.0 field and adds
  `projection_state` (`none`, `stale`, `current`) and `projection`: agent, framework,
  outcome, tags, duration, turns with the framework's reported latency and the audible
  gap at the agent (± uncertainty, or `not_measurable` with a reason; never 0 for
  unknown), signals (talk time, overlap, barge-ins, talk-overs, long silences, dead air),
  cost items, the transcript placed on the call's axis, and the playable media. It returns
  an `ETag` and answers `If-None-Match` with 304. See [the call view](docs/calls.md).
- **Call projections.** A background job recomputes each Call's projection after each
  Revision, each analysis result and each price change, from the stored spans, the
  Recordings' analyses and the prices. Agent, outcome and tags come from the new
  `earshot.agent.name`, `earshot.call.outcome` and `earshot.call.tags` attributes, with
  fallbacks. See [ADR 0008](docs/adr/0008-recording-analysis-and-call-projections.md).
- **Calls list filters.** `GET /v1/calls` takes `agent`, `outcome`, `tag` (repeatable,
  all must match), `min_/max_reported_latency_ms`, `min_/max_audible_gap_ms`,
  `min_/max_cost_nano_usd` and `q`, a transcript substring (3 to 256 characters, `%` and
  `_` literal, ASCII case-insensitive; 422 `EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH` under
  `metadata_only`). A filter never matches an unknown value. `max_cost_nano_usd` matches
  complete costs only, and the page reports `excluded_incomplete`. Items carry the
  projection's summary.
- **`GET /v1/calls/{id}/spans/{trace_id}/{span_id}`**: one stored span, with each
  attribute's semantic key and capture class; 404 unless its trace belongs to the Call.
- **Recording analysis.** Each stored Recording is analysed once, by the server's
  maintenance loop, in a child process of its own: per-channel speech runs from Silero
  VAD with each edge refined to 10 ms and given an uncertainty, the noise floor,
  exact-zero runs, and audiowaveform `.dat` peaks. Crashes, timeouts and out-of-memory
  exits are retried after 1, 4 and 16 minutes, then `failed`; undecodable audio is
  `unavailable` with a code. Recordings stored before the upgrade are queued.
- **The `signals` extra** (numpy, onnxruntime, soundfile; the Silero VAD v6.2.3 model,
  MIT, ships in the package). Without it the server runs, analyses are `unavailable`
  (`EARSHOT_SIGNALS_EXTRA_REQUIRED`), and they are requeued at the next start once it is
  installed.
- **`GET /v1/recordings/{id}/peaks`** (`private, no-cache`, `ETag`, 304), and for a mono
  user/agent pair on one timeline a stereo FLAC rendition at
  `GET /v1/recordings/{id}/rendition` (Range) and `…/rendition/peaks`.
- **Cost** in integer nano-USD: a lower bound from priced items, an estimate (STT seconds
  from the recording or call duration), and a count of unknown items. Built-in prices are
  regular, dated list prices with their source, never promotional ones, for Deepgram,
  Groq, OpenAI, Cartesia and ElevenLabs models. A Project's own prices win over the
  built-ins, and a price change reprices from stored usage. See
  [ADR 0009](docs/adr/0009-cost-model.md).
- **Prices:** `GET /v1/prices` and `POST /v1/prices` (project-scoped, append-only,
  effective-dated; hosted JWTs get 403), and `earshot prices add|list` (`--builtin` for
  operators).
- **`earshot db enable-transcript-index`**: an optional Postgres `pg_trgm` index for
  transcript search.
- **Stereo recordings.** `POST /v1/recordings` takes `channel_speakers`
  (`["user","agent"]`, each at most once) with `speaker: "mixed"`; the upload's WAV, FLAC
  or Ogg Opus header must have that many channels. FLAC and Ogg Opus uploads are
  inspected and get a `duration_ms`. New codes: `EARSHOT_INVALID_CHANNEL_SPEAKERS`,
  `EARSHOT_RECORDING_INVALID_FLAC`, `EARSHOT_RECORDING_INVALID_OGG`.
- **Contract `MediaRef.channel_index`.** A Recording with a channel map becomes one audio
  stream and one media reference per channel.
- **LiveKit over OTLP.** The server builds Revisions of LiveKit Agents calls with the same
  `LiveKitAdapter` that runs in process, and their turn facts match. Transcripts take user
  lines from `user_turn` and agent lines from `agent_turn`'s played text, never from LLM
  output, so preemptive replies LiveKit discarded never appear.
- **`earshot.client`**: create and upload recordings, resolve and get calls, and
  `tracer_metadata` for LiveKit's `set_tracer_provider(metadata=…)`. Standard library
  only, with httpx when installed.
- **`@earshot/node` 0.3.0**, a new npm package for Node voice agents (ESM-only, Node
  22.12 or later, `@opentelemetry/api` as its one peer):
  - `EarshotSpanProcessor` stamps `earshot.session.id` on every span, from
    `withSessionKey(key, fn)`, `setSessionKey(context, key)` or a fixed key; a span's own
    key wins; the key is never sent as baggage; invalid keys throw where they are set.
  - `EarshotClient`: `createRecording` (stereo `channel_speakers` included),
    `uploadRecording` (streamed from disk with a sha-256 `Content-Digest`),
    `getRecording`, `resolveCall`, `getCall`; `EarshotClient.fromEnvironment()` reuses the
    OTLP exporter's endpoint and `Authorization` header; refused requests throw
    `EarshotError` with Earshot's stable code.
  - `recordingIdFor` names the same Recording as Python's `recording_id_for`.
  - Response types generated from the OpenAPI spec.
- **Viewer: calls list** (`/calls`). Filters by agent, outcome, tags, reported latency,
  audible gap, cost and transcript text, and pages with "load more". A call that isn't
  projected yet shows no metrics rather than zeros, and a cost cap reports how many calls
  it left out for incomplete cost.
- **Viewer: call view** (`/calls/{call_id}`). A waveform player (wavesurfer.js 8) with
  one lane per speaker, drawn from the server's peaks and streaming the recording by
  Range (a mono pair plays as its stereo rendition); a transcript synced to playback,
  where clicking a line seeks; per-turn reported latency beside the audible gap; signals;
  cost (lower bound, unknown items, estimate); LLM, tool and error details loaded per
  span. A call that isn't projected yet shows its span transcript.
- **`@earshot/viewer-ui`:** `ObserveRoute` gains the `calls` and `call` kinds. The package
  depends on `wavesurfer.js` ^8, which loads only with the player.
- **Catalog revision `0005_projections`** (forward-only).
- **`earshot.call_span_facts`**: the usage→meter table for Pipecat and LiveKit and
  per-turn anchors; **`earshot.leased_jobs`**: a generic leased-job helper for
  catalog-backed background jobs.
- **Examples:** the Pipecat example uploads one stereo WAV; the LiveKit example's
  `--upload` creates its recording at session start and uploads it from
  `on_session_end`.
- **`scripts/viewer_smoke.py`**: the bundled viewer and the API its call views use,
  checked on the two replayed calls, against the source tree or an installed artifact.
- **Docs:** [the call view](docs/calls.md), ADR 0008 (analysis and projections), ADR 0009
  (cost), and LiveKit, `earshot.client` and Node sections in the
  [OTLP quick start](docs/otlp-quickstart.md).

### Changed

- **Pipecat 1.6.** The `pipecat` and `pipecat-groq` extras require Pipecat `>=1.6,<1.7`, and
  the OTLP example runs on it. The real-provider done-when e2e passes on a real Pipecat 1.6.0
  call; the captured replay fixture is still a Pipecat 1.5 call.
- **The `livekit` extra allows `livekit-agents>=1.6.5,<1.9`.** Keys are read by
  attribute, never by version.
- **LiveKit job-tail spans** after `session_close` (`agent_session`, `on_session_end`,
  `session_end_upload`, `room_disconnect`, `job_shutdown`, `job_entrypoint`,
  `shutdown_callback`) are stored with their call, but no longer reopen a closed call or
  delay its close. The `session_close` span name is kept rather than hashed.
- **Purge, retention and project deletion** also remove a Call's projection, tags,
  transcript lines and analyses, and its Recordings' derived blobs (peaks, renditions);
  project deletion also removes the Project's own prices.
- **The container image** installs the `signals` extra (142 MB, from 94 MB), runs as
  uid/gid 999, and sets `ORT_DISABLE_TELEMETRY=1`. The server also turns onnxruntime's
  telemetry off before it first imports it; it makes no outbound connections of its own.
- **Viewer media custody panel:** Recordings Earshot stores are labelled as held by
  Earshot with an Earshot-measured digest, and are played in the call view. Media held
  elsewhere is still a reference only.
- **`CALL_PROJECTION_VERSION` is 6**, so every existing projection is recomputed.
- **A mono pair is one timeline only when unambiguous.** The analysis and the projection
  share one rule (`call_timeline.one_timeline_pairs`): a user and an agent mono Recording
  with the same start and durations within 20 ms, and no other mono user or agent
  Recording at that start. Otherwise there is no rendition, and the turn is
  `not_one_timeline`.
- **The analysis timeout is at most 1 hour**, and 15 minutes when the declared duration is
  more than the file's bytes could hold; the lease is at most 61 minutes.
- **The clients never follow a redirect**, so the API key goes only to the URL given. A
  redirect was followed before.

### Fixed

- **Projection:**
  - A bad value on one Call no longer stops other Calls being projected. The Call is set
    aside with `EARSHOT_PROJECTION_INPUT_INVALID`, `EARSHOT_PROJECTION_OUT_OF_RANGE` or
    `EARSHOT_PROJECTION_WRITE_REFUSED` and retried with backoff.
  - A value outside the catalog's range (a time or latency in nanoseconds past signed
    64 bits, a cost total, a producer-reported duration) is unknown, never clamped.
  - U+0000 is never stored.
  - A projection shows exactly its Revision's evidence: spans and Recordings stored later
    wait for a later Revision (new `first_revision` columns in migration `0005`).
  - Per-channel media references are made only for WAV, FLAC and Ogg Opus.
  - The decoded duration is preferred over the header's.
- **Analysis:**
  - A failure outside the child (spool, reading the Recording, writing the result) is
    retried, then ends `failed` with `EARSHOT_ANALYSIS_SPOOL_FAILED`,
    `EARSHOT_ANALYSIS_SOURCE_UNREADABLE`, `EARSHOT_ANALYSIS_CRASHED` or
    `EARSHOT_ANALYSIS_STORE_FAILED`; it used to leave the job `running`. A lease that
    runs out counts as a failed attempt (`EARSHOT_ANALYSIS_LEASE_EXPIRED`).
  - Lease tokens are unique, so a claim on a purged job cannot write to its replacement.
  - A derived blob from a write that landed after its reservation was drained is deleted.
  - WAV header parsing is linear in the header's size.
- **Uploads:** an empty WAV (`data` size 0, or no whole sample) or FLAC (STREAMINFO with
  no samples) is refused with 422 `EARSHOT_RECORDING_INVALID_WAV` or
  `EARSHOT_RECORDING_INVALID_FLAC`.
- **Clients:** a redirect, a non-JSON answer and an answer cut short are `EarshotError`
  in the Python and Node clients.

- Under full capture, the in-process LiveKit adapter refused LiveKit 1.6 `agent_turn` spans
  (user input, instructions and reply) and every `tts_request` span (text plus an
  audio-duration metric): "one record cannot mix multiple sensitive capture classes".
- LiveKit's TTS and STT audio-duration metrics stay quality measurements and are no
  longer copied onto the operation.

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
- **Earshot now holds media bytes.** Recordings for `full` Projects are stored in
  Earshot's blob store. `MediaRef`s naming other custodians keep their earlier meaning.
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
  and [server configuration](docs/configuration.md).

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
- Removed the superseded TypeScript prototype, `packages/schema` and `packages/analysis`.
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
