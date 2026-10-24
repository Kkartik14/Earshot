# Production notes

These are the operational rules for running Earshot 0.3.0 for a team: Postgres for the
catalog, S3 for recordings, and a Collector in front. [Storage and blobs](./storage-and-blobs.md)
explains the backends, and [server configuration](./configuration.md) lists every
variable.

```text
voice agents ──OTLP/HTTP──▶ Collector (persistent queue) ──▶ earshot serve ──▶ Postgres
      └──────── recordings (POST, PUT) ─────────────────────────┘      └──────▶ S3
```

## One server, one worker

Run exactly one Earshot server per database, with one worker. Multiple replicas and
multiple workers are not supported in 0.3.0.

- On Postgres the server takes a session advisory lock at start. On SQLite it takes an
  exclusive file lock on `<data-dir>/.server.lock` and holds it until it exits. A second
  `earshot serve` on the same database or data directory exits with
  `EARSHOT_DATABASE_IN_USE`. This includes a restart that overlaps the old process: start
  the new server after the old one has exited.
- `uvicorn apps.ingest.app:app --workers 2` is refused the same way: the second worker
  exits with status 3 and uvicorn stops the whole server. The uvicorn parent itself exits
  with status 0, so do not rely on its exit status to detect this.
- Do not fork after import. Gunicorn's `--preload` is not supported.
- Never delete `<data-dir>/.server.lock` while a server runs. The lock is held on the open
  file, so a new file at that path lets a second server start on the same data directory
  (accepted limit: it takes an operator's deletion).
- A blob location (`EARSHOT_BLOB_URL`) belongs to one server too. Two servers with
  different catalogs must not share an S3 prefix or a `file://` root: each one's cleanup
  removes the other's uploads in progress. The lock does not detect this.

CLI commands are not servers. `earshot api-key revoke`, `earshot project set-capture` and
the other admin commands are safe while the server runs; on Postgres they wait for the
server's current write, then run.

## No transaction-pooling proxy

Earshot holds Postgres session advisory locks: one for the server, and one for the
duration of each write. A proxy that hands a session's statements to different server
connections breaks both. Do not put PgBouncer in transaction or statement mode between
Earshot and Postgres. Session mode works, and so does a direct connection.

Plan for `EARSHOT_DB_POOL_SIZE` connections (default 8) plus two more per server, and one
pool plus one per CLI command while it runs.

## Supervise it: lock loss exits the process

Every second, the server checks that its lock session is still alive. If the session is
gone, for example after a database failover, a network partition, or an administrator's
`pg_terminate_backend`, the server logs `EARSHOT_DATABASE_LOCK_LOST` and exits at once with
status 70. It does not try to take the lock back, because another server may already
hold it.

For an orchestrator this means:

- **Restart on failure.** Use a restart policy (`restart: unless-stopped` in Compose,
  `Restart=on-failure` in systemd, the default in Kubernetes). The restarted server takes
  the lock again, or exits with `EARSHOT_DATABASE_IN_USE` while another server holds it.
- **In-flight requests are dropped.** A request gets its 200 only after its transaction
  commits, so a dropped request was not stored and the client's retry is safe. This is why
  the Collector below matters.
- **Detection time.** A terminated session is detected within about a second. A silent
  network partition takes up to about 10 seconds (TCP keepalives).

| Exit status | Meaning                                                                                                                           | Restart?              |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| 70          | `EARSHOT_DATABASE_LOCK_LOST`                                                                                                      | Yes                   |
| 2           | `earshot serve` refused to start: an invalid setting, a second server, or a schema it will not open. The message names the cause. | Not until it is fixed |
| 3           | The ASGI module refused to start: an invalid setting or a second server                                                           | Not until it is fixed |

Each write also holds a lock on its own session. If that session dies in the middle of a
write, Earshot logs `EARSHOT_WRITE_LOCK_LOST` and the write finishes without the lock; the
next write reconnects. This is a known 0.3.0 limitation.

## Put a Collector in front

The Python OTLP exporter retries a failed batch for about 10 seconds and then drops it. It
ignores `Retry-After`. Earshot answers 503 with `Retry-After: 3` when it is busy, and it is
briefly unreachable while it restarts. An OpenTelemetry Collector with a persistent queue
keeps those batches until Earshot accepts them.

The `file_storage` extension is in the Collector's contrib distribution
(`otelcol-contrib`):

```yaml
extensions:
  file_storage:
    directory: /var/lib/otelcol/queue

receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  otlphttp/earshot:
    endpoint: http://earshot:4319 # the exporter appends /v1/traces
    headers:
      Authorization: Bearer ${env:EARSHOT_API_KEY}
    sending_queue:
      enabled: true
      storage: file_storage
    retry_on_failure:
      enabled: true
      max_elapsed_time: 0 # keep retrying while the queue holds the batch

service:
  extensions: [file_storage]
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp/earshot]
```

Then point your agents at the Collector (`OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318`).
`EARSHOT_API_KEY` here is a variable of the Collector, holding an Earshot API key; a key
also picks the Project, so use one Collector pipeline per Project. Keep the queue
directory on a persistent volume. Do not add a filtering or sampling processor that drops
spans of a call: Earshot needs every span of a trace, including the end marker.

Recordings do not go through the Collector. Agents upload them to Earshot directly.

## S3 lifecycle rule

Earshot aborts multipart uploads it gives up on, and removes unfinished ones under its
prefix when it starts and hourly after that. A crash can still leave parts behind, and S3
bills for them until they are aborted. Add a lifecycle rule that aborts incomplete
uploads:

```json
{
  "Rules": [
    {
      "ID": "earshot-abort-incomplete-uploads",
      "Status": "Enabled",
      "Filter": { "Prefix": "earshot/" },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 1 }
    }
  ]
}
```

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket <bucket> --lifecycle-configuration file://lifecycle.json
```

Use the same prefix as `EARSHOT_BLOB_URL`. Do not add expiration rules for Earshot's
objects: Earshot deletes recordings, and the peaks and renditions derived from them,
itself when a Call is purged or its Project is deleted (in 0.3.0 no Call expires; see
[retention](./privacy.md#calls)). If bucket
versioning is on, a deletion leaves a noncurrent version behind; add a
`NoncurrentVersionExpiration` rule, or turn versioning off, to bound how long deleted
recordings survive. See [privacy](./privacy.md#erasure-limits).

## Recording analysis and the `signals` extra

The server analyses every stored Recording once, for the [call view](./calls.md): speech
runs per channel with their uncertainty (the audible gap at the agent and the signals),
waveform peaks, and a stereo FLAC rendition for a mono user/agent pair. Install the
`signals` extra (numpy, onnxruntime, soundfile, and the Silero VAD v6.2.3 model, MIT,
which ships inside the package):

```bash
pip install 'earshot-observability[server,postgres,s3,signals]'
```

The container image includes it. It is 142 MB with the extra, against 94 MB without
it. Without the extra the server runs, and every analysis is
`unavailable` with `EARSHOT_SIGNALS_EXTRA_REQUIRED`: Calls keep their transcript and
reported latency, and no turn's audible gap is measured. A server started with the extra
requeues those analyses.

**One child process per analysis.** The server never loads numpy or onnxruntime itself.
It runs one analysis at a time, each as `python -m earshot.signals.child` in a process of
its own:

- its own session, at `nice 10`, with one thread per numeric library (onnxruntime on the
  CPU, one thread), no core files, and its output discarded;
- on Linux, an address-space limit of 4 GiB (`RLIMIT_AS`); macOS cannot set one, so a
  macOS child is uncapped;
- it exits within a second if the server that started it dies, and a server that stops
  kills it; its lease is released at the next start;
- it reads a copy of the Recording spooled in `<data-dir>/analysis-tmp/` (owner-only),
  removed when it ends and swept at every start. Leave room there for the largest
  Recording you accept (`EARSHOT_RECORDING_MAX_BYTES`, 512 MiB by default).

**Timeouts and retries.** An analysis may run for 30 s plus a quarter of the audio's
duration, at most 1 hour (15 minutes when the duration is unknown, or more than the
file's bytes could hold); its lease is that plus 60 s. A crash, a timeout, an
out-of-memory exit, or a failure on the server's side of the job (the spool directory,
reading the Recording, writing the result) is retried after 1, 4 and 16 minutes, then is
`failed`. A lease that runs out before its holder wrote an outcome counts as a failed
attempt too, so no job is retried forever. A store that is only briefly busy delays the
write and costs no attempt; one that stays busy past the retries leaves the job leased, and
only the lease's expiry counts. Audio it cannot analyse is `unavailable` at once, never
guessed:

| State         | Codes                                                                                                                                                                                                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `unavailable` | `EARSHOT_SIGNALS_EXTRA_REQUIRED`, `EARSHOT_ANALYSIS_DECODE_FAILED`, `EARSHOT_ANALYSIS_UNSUPPORTED_CODEC` (WebM, which libsndfile cannot read), `EARSHOT_ANALYSIS_NO_AUDIO`, `EARSHOT_ANALYSIS_CHANNEL_MISMATCH`, `EARSHOT_ANALYSIS_MODEL_MISMATCH`, `EARSHOT_ANALYSIS_SOURCE_MISSING` |
| `failed`      | after three retries: `EARSHOT_ANALYSIS_CRASHED`, `EARSHOT_ANALYSIS_OUT_OF_MEMORY`, `EARSHOT_ANALYSIS_TIMEOUT`, `EARSHOT_ANALYSIS_SOURCE_UNREADABLE`, `EARSHOT_ANALYSIS_STORE_FAILED`, `EARSHOT_ANALYSIS_SPOOL_FAILED`, `EARSHOT_ANALYSIS_LEASE_EXPIRED`                               |

**What it costs**, measured on an Apple M4 Pro (macOS, one run each; Linux not measured):

| 60 minutes of audio                         | Wall time | CPU per audio minute | Child peak RSS | Peaks   |
| ------------------------------------------- | --------- | -------------------- | -------------- | ------- |
| one mono 16 kHz WAV track (Pipecat)         | 9.9 s     | 0.16 s               | 103 MiB        | 0.45 MB |
| the same, plus a mono pair's FLAC rendition | 11.3 s    | 0.18 s               | 103 MiB        | 0.45 MB |
| one stereo 48 kHz Ogg Opus file (LiveKit)   | 52.2 s    | 0.85 s               | 169 MiB        | 2.7 MB  |

Memory stays flat with the call's length: the audio is decoded in 10-second blocks.
Upgrading to 0.3.0 queues an analysis for every Recording already stored, and a later
release with a changed algorithm requeues older results at start, 500 at a time; plan
for that CPU after an upgrade.

**Telephony audio.** At 8 kHz the voice activity model's edges are less precise: on the
fixtures they moved up to about 160 ms from the 16 kHz edges. Each edge's uncertainty
reports this, so expect larger ± values.

onnxruntime's own usage telemetry is turned off; see
[privacy](./privacy.md#outbound-connections).

## Call projections and prices

The `call-projection` job keeps each Call's projection (the list's columns and the call
view) current. It recomputes a Call after each Revision, each analysis result and each
price change, 20 Calls per pass, outside the store's write lock, and writes only if the
Call has not changed meanwhile. A Call whose projection cannot be computed is logged
with `EARSHOT_PROJECTION_FAILED` and its code (`EARSHOT_PROJECTION_INPUT_INVALID`,
`EARSHOT_PROJECTION_OUT_OF_RANGE` or `EARSHOT_PROJECTION_WRITE_REFUSED`), and set aside:
it is tried again after 1 minute, then 4, then every 16, or as soon as it changes. The
other Calls are projected meanwhile. This record is held in memory, so a restart tries
the Call once more. After an upgrade, every Call with a Revision is projected once.

A price change reprices every affected Call from its stored usage, without reading its
spans: 10,000 Calls took 9.9 s on SQLite on the machine above. The built-in prices are
seeded at every start, idempotently; operators add rows with
`earshot prices add --builtin` and Projects with `earshot prices add --project` or
`POST /v1/prices` (see [the call view](./calls.md#prices)).

A 60-minute Call's view measured 35 to 38 KB gzipped (685 to 728 KB of JSON), served
with a p95 of 8 to 14 ms on SQLite, in both the Pipecat and the LiveKit shape.

## Transcript search index

The calls list's `q` searches every placed transcript line of the Project. Without an
index this is a scan: on SQLite, 10,000 Calls with 200,000 lines took at most 30 ms
(p95) for a word in few Calls, growing with the number of lines.

On Postgres, an optional trigram index serves the same `ILIKE` search:

```bash
earshot db enable-transcript-index
```

It runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` and creates the GIN index
`earshot_opt_call_transcripts_text_trgm` on `call_transcripts.text`. The database role
needs the right to create the extension, or an administrator must create it first. The
command is idempotent and is not part of the migration chain: schema checks ignore
`earshot_opt_*` indexes, and an upgrade keeps it. It takes the store's write lock and
builds the index in one statement, so other writes wait while it builds, and an OTLP
request that waits more than 2 s is answered 503 (a Collector in front retries it). Run
it while traffic is low. Postgres search at scale, with and without the index, has not been
measured yet.

## Readiness and `projections_pending`

| Endpoint       | Use                                                                                                                                                   |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /healthz` | Liveness. 200 `{"status": "ok"}` while the process runs. It does not check storage.                                                                   |
| `GET /readyz`  | Readiness. 200 `{"status": "ready", "projections_pending": N}`, or 503 `{"status": "unavailable"}` when the catalog or artifact store cannot be read. |

Both are unauthenticated and return no Project data. The container image's health check
uses `/readyz`.

`projections_pending` counts Incidents whose read models (graph rows, Turn Facts,
retention and export columns) were built by an older release. After an upgrade every
Incident is pending; the server re-projects them in the background in batches of 100, and
the count falls to 0. It never makes the server unready, so a large upgrade does not hold
traffic back. Until it reaches 0, Turn Facts and graph reads for older Incidents can be
incomplete. An Incident whose artifact cannot be read stays pending, the server logs
`EARSHOT_PROJECTION_FAILED` and retries a minute later, and `earshot db reproject` names it.

## Backups

A backup is a set that must be taken and restored together:

| Backend  | What to copy                                                                                                                                                                                                                     |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQLite   | The whole data directory: `earshot.sqlite3` with any WAL and SHM files, `objects/`, `instance-correlation.key`, and `blobs/` (or the `file://` blob root if it is elsewhere). `analysis-tmp/` is scratch and need not be copied. |
| Postgres | A `pg_dump` of the database, the blob store (bucket prefix or directory), and the correlation key.                                                                                                                               |

For SQLite, stop Earshot first and follow [the backup procedure](./storage.md#backup-and-restore-procedure).

For Postgres:

```bash
pg_dump --format=custom --file earshot.dump "$EARSHOT_DATABASE_URL"
aws s3 sync s3://<bucket>/earshot/ ./earshot-blobs/
```

`pg_dump` is consistent on its own, so the database can be dumped while Earshot runs.
Take the dump first and copy the blobs second. Each upload writes a new object before its
Recording row commits, and objects are never overwritten, so every stored Recording in the
dump has its object in the later copy. A Call purged between the two steps comes back
from the dump without its audio.

To restore: create an empty database, `pg_restore --no-owner --dbname "$EARSHOT_DATABASE_URL"
earshot.dump`, copy the blobs back to the same `EARSHOT_BLOB_URL`, set the same correlation
key, start Earshot, and run `earshot db reproject --all` to verify every artifact.

Backups are copies of evidence. A Call purged today stays in every backup taken before the
purge until that backup expires. Set backup retention with your privacy obligations in
mind.

## Upgrades

1. Back up, as above.
2. Install the new release.
3. Run `earshot db upgrade`, or let `earshot serve` migrate on start. Start the server with
   `--no-auto-migrate` if migrations must be an explicit deploy step.
4. Watch `/readyz` until `projections_pending` is 0.

Upgrading to 0.3.0 adds the projection tables (catalog revision `0005_projections`) and
queues every stored Recording for analysis. Install the `signals` extra before you
start the new server, or the analyses become `unavailable` until a server with it
starts.

Migrations are forward-only. To roll back, restore the backup. Upgrading from 0.1.x also
changes every existing Project's Capture Preset to `metadata_only`; see
[upgrading from 0.1.x](./storage-and-blobs.md#upgrading-from-01x).

## Exposure and limits

- Off loopback, use API keys and a TLS proxy (`EARSHOT_BEHIND_TLS_PROXY=true`). The
  [host rule](./otlp-quickstart.md#api-keys-and-the-host-rule) shows what each setting
  accepts. Never set `EARSHOT_TRUST_LOCAL_NETWORK` on a public interface.
- Request limits: `EARSHOT_MAX_BODY_BYTES` bounds a compressed request (16 MiB by
  default); a decompressed OTLP request is bounded at 32 MiB (protobuf) or 4 MiB (JSON);
  a Recording at 512 MiB. Streaming a 1 GiB Recording grows the server's memory by at most
  64 MiB. A `Range` header longer than 256 bytes is answered 416 without being read (no
  single range needs more). An OTLP span is rejected on its own, and counted in
  `rejected_spans`, when it has an attribute key longer than 1024 bytes (on the span, an
  event, a link, its resource or its scope), an instrumentation scope name longer than
  256 bytes, scope attributes over 16 KiB serialized, or a resource over 64 KiB
  serialized (its attributes and its schema URL included), when its scope is not
  among the first 256 distinct instrumentation scopes of its request, and when it would
  take what its request stores more than once past 8 MiB (see "What one OTLP request
  can store" below). A Call holds at most 16 Recordings; creating a 17th is answered
  409 `EARSHOT_RECORDING_LIMIT`.
- **A Recording upload holds its Call while it streams.** The Call is not closed while
  a `PUT` of one of its Recordings is sending bytes, so its first Revision includes the
  audio. Two bounds end a `PUT` with 408 and stop it holding its Call: no body bytes for
  60 seconds (`EARSHOT_UPLOAD_IDLE_TIMEOUT`: a client whose connection died without
  closing), and `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` (600 by default) since the `PUT`
  started (`EARSHOT_UPLOAD_DEADLINE_EXCEEDED`: a client that sends a trickle). At 600
  seconds a 512 MiB Recording needs about 7.2 Mbit/s; raise the setting for slower
  uploaders. A proxy in front may end idle bodies sooner; the client then retries with a
  new `PUT`.
- **OTLP memory.** The server works on at most `EARSHOT_DB_POOL_SIZE` OTLP requests at
  once (default 8). Each holds its request three to four times over while it decodes,
  filters and stores it, so plan for about 4 × the decompressed limit per request: with
  the defaults, about 8 × 32 MiB × 4 ≈ 1 GiB above the server's baseline. A small gzip body
  can reach that, because the 32 MiB limit applies after decompression: a 140 KB body that
  decompresses to 31 MiB was measured at about 100 MiB per request, and eight at once at
  about 970 MiB. Size the container for this, or lower `EARSHOT_OTLP_MAX_PROTOBUF_BYTES`
  and `EARSHOT_DB_POOL_SIZE`. A request also needs about as much memory as it stores
  (next item).
- **What one OTLP request can store.** Part of a request is stored more than once: each
  span's row holds its scope name, and a resource and scope are stored again with each
  distinct pair of them (a scope's version and the schema URLs have no bound of their
  own; this one covers them). A request's accepted spans may store at most 8 MiB this way;
  the spans that would take it further are rejected through `partial_success`, and the
  rest of the request is stored. Ordinary exports use kilobytes of it. Beyond it, a
  request stores its spans: 512-span requests of real Pipecat spans stored 1.3 times
  their size in one trace, and 1.9 times over 512 traces; the smallest spans there are
  (IDs and times only, each a trace of its own) stored 4.6 times their size. The worst
  request within the bounds, two 64 KiB resources under 256 scopes of one minimal span
  each (0.16 MB), asked for 32 MiB before this bound; it now stores 8.4 MB, in 0.04 s on
  SQLite. To bound one request's cost further, lower `EARSHOT_OTLP_MAX_PROTOBUF_BYTES`
  (and `EARSHOT_OTLP_MAX_JSON_BYTES`, 4 MiB by default).
- **Materializer child process.** The server builds each Call's Revision in one
  long-lived child process, started with the `spawn` method before the server serves.
  Building in a separate process keeps a large close from stalling the server's OTLP
  acks.
  - **Memory.** The child builds one Call at a time, so its peak follows the largest
    Call, and it grows with the square of the Call's turns, because every LLM span
    carries the conversation so far. Measured on synthetic Pipecat calls:

    | Turns | Stored snapshot | Child peak                 |
    | ----- | --------------- | -------------------------- |
    | 100   | 2.4 MiB         | about 90 MiB               |
    | 300   | 21 MiB          | about 150 MiB              |
    | 1,000 | 237 MiB         | 600 to 700 MiB             |
    | 2,000 | about 950 MiB   | about 2.5 GiB (projection) |

    For other lengths, the snapshot is about 2.4 MiB × (turns ÷ 100)², and the child
    peaks at about 80 MiB plus 2.5 to 3 times the snapshot. While it sends the snapshot,
    the server also holds it: plan for the server's own peak to rise by up to twice the
    snapshot (measured at 1,000 turns on a freshly started server: +278 and +299 MiB on
    SQLite, +280 to +480 MiB on Postgres).
    On macOS the child returned to about 100 MiB after a build; on Linux this was not
    measured. Size the container for the server, the child's peak for your longest
    calls, and the server's transient.

  - A host that starts the server from its own Python script must guard that script with
    `if __name__ == "__main__":`, because `spawn` imports the script again.
  - If the child can't start, the server refuses to start: it logs
    `EARSHOT_MATERIALIZER_START_FAILED` and exits with status 3, under both
    `earshot serve` and `uvicorn apps.ingest.app:app`. When the process could not be
    created at all, the line names the error's type and errno, for example
    `OSError, errno 24` (the host is out of file descriptors).
  - If the child crashes, or does not answer a build within 600 seconds (sending it the
    snapshot included, so a stopped child cannot block the server), it is killed, the
    Call records `EARSHOT_MATERIALIZE_WORKER_FAILED` and is retried with backoff, and the
    next build starts a new child.
  - At shutdown a build in progress gets 10 seconds; then the child is killed
    (`EARSHOT_MATERIALIZER_STOPPED`), and the next start retries the Call. A stalled
    child never keeps the server from stopping.
- **OTLP latency while a very large Call closes on Postgres (accepted limit).** Closing
  a Call reads its stored snapshot and then stores the Revision under the write lock.
  For a 600- to 1,000-turn Call that lock hold is about 100 to 230 ms, so one or two
  OTLP acks during each such close take 100 to 250 ms; none is refused with 503. At 300
  turns or fewer the ack p95 during a close was 56 ms (one exporter sending 512-span
  requests back to back, measured on a loaded machine). Exporters time out after 10
  seconds, so this costs latency, not data. On SQLite, with one exporter, the ack p95
  during a 1,000-turn close was 48 ms.
- **Requests that name thousands of new traces (accepted limit).** A request costs more
  the more new traces its spans start. On Postgres, 512 spans over 512 new traces take
  about 120 ms; 8,192 spans over 8,192 new traces took 1.4 to 2.5 s each. OpenTelemetry
  SDKs send at most 512 spans per request by default (`OTEL_BSP_MAX_EXPORT_BATCH_SIZE`).
  If you add a `batch` processor to the Collector, keep its `send_batch_max_size` near
  that. A request's spans are stored in one transaction, so its write-lock hold grows
  with its spans: on SQLite about 21 µs per span when each starts a trace (200,000 such
  spans, 9.2 MiB, took 4.2 s). A 32 MiB request of the smallest spans, about 650,000,
  would hold the lock for about 14 s (extrapolated), and other writes are answered 503
  after waiting 2 s for it. Lower `EARSHOT_OTLP_MAX_PROTOBUF_BYTES` to bound this.
- **Admission before the body is read.** An OTLP request takes its admission slot (one of
  `EARSHOT_DB_POOL_SIZE`) before its body is read. This bounds the memory held by request
  bodies in flight, at the cost of letting a slow client keep a slot while it uploads.
  Behind a Collector or a TLS proxy that buffers requests, this does not matter.
- **Send protobuf.** Decoding OTLP/JSON costs the server about 100 times the CPU of
  protobuf (about 37 ms against 0.3 ms for 512 spans), and that CPU is spent in the
  process that serves every request. The Python exporter and the Collector's `otlphttp`
  exporter send protobuf by default. JSON is accepted, up to 4 MiB, for clients that
  cannot send protobuf.
- **Failed API-key checks are not throttled (known limit).** A key's secret is checked
  with scrypt, about 22 ms of CPU and 16 MiB of memory per check. A correct credential is
  remembered after its first check; a wrong secret for an existing key ID is checked in
  full every time. A client that floods the server with wrong secrets for a real key ID
  therefore slows every other request (32 such clients raised a legitimate OTLP request's
  median latency from 15 ms to 190 ms in a measurement). An unknown or revoked key ID is
  refused before scrypt. 0.3.0 has no per-key limit on failed attempts: rate-limit 401
  answers in the TLS proxy, and revoke and replace a key whose ID is being abused.
- The server warns with `EARSHOT_SLOW_MUTATION` when a write holds the store lock for more
  than a second.
