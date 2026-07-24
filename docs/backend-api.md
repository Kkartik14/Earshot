# Backend API

Default address: `http://127.0.0.1:4319`. Port 4318 is intentionally left available
for a standard OTLP/HTTP receiver.

The reproducible machine contract is
[`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json). Incident request
bodies reference the generated incident schema; analysis responses reference
[`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json).

Tokenless development is loopback-only and rejects non-loopback `Host` headers. Remote
deployments must explicitly declare a trusted TLS proxy and authenticate `/v1/*` with a
project API key, an expiring server-created viewer session, or the legacy
default-project bearer token. API keys are exchanged once for an HttpOnly,
SameSite=Strict viewer cookie; unsafe cookie-authenticated methods require the
in-memory CSRF token, and logout, expiry, or issuer-key revocation invalidates the
session. Every repository call is
scoped from that principal; an unknown incident in another Project is indistinguishable
from a missing incident. The request middleware checks the actual ASGI listener as well
as declared configuration and refuses both `/v1/*` and `/hooks/v1/*` on an unexpected
non-loopback plaintext bind.

There is one explicit exception for single-machine self-hosting: `serve
--trust-local-network` (env `EARSHOT_TRUST_LOCAL_NETWORK`). It permits an
unauthenticated non-loopback bind — intended for a loopback-mapped container
(`docker -p 127.0.0.1:PORT`), which keeps the listener on a trusted boundary while still
requiring a loopback `Host` header. Under it, `/v1/*` is served anonymously (unless a
token is also configured, which is still enforced), and `/v1/auth/session` reports
`authentication_required: false` so the bundled viewer loads without a project key. A
single predicate governs middleware enforcement, the session gate, and the generated
OpenAPI `security`, so the machine contract always matches runtime. Never enable it on a
public interface.

SDK requests assert `X-Earshot-Project-Id`. When present, the backend compares it with
the project selected by the bearer credential (or local default project) and returns
`403 EARSHOT_PROJECT_MISMATCH` on disagreement. Authentication remains authoritative;
the assertion cannot select or override a project.

Bundle identifiers occupy one installation-wide namespace. Producers should use UUIDv4,
UUIDv7, or Connector-generated collision-resistant IDs. Projects are single-organization
authorization scopes in this alpha, not hostile SaaS tenant boundaries.

Provider `/hooks/*` routes are a separate trust boundary. They do not accept Earshot
bearer credentials as provider proof and do not return Project identifiers.

## Media types

```text
application/vnd.earshot.incident+protobuf
application/vnd.earshot.incident+json
```

`application/x-protobuf` and `application/json` are accepted aliases. Incident and
validation requests may use one `Content-Encoding: gzip` member. Both compressed and
decompressed sizes are bounded; malformed, concatenated, or trailing gzip data is
rejected before contract decoding. Signed provider hooks continue to authenticate the
exact uncompressed delivery bytes defined by each provider.

## Endpoints

### `GET /healthz`

Process liveness. It does not imply storage is writable.

### `GET /readyz`

Checks SQLite and object-store readiness. Returns 503 when unavailable.

### `POST /hooks/v1/connectors/{endpoint_id}`

Accepts a bounded `application/json` Provider Delivery. The configured Connector verifies
the provider credential/signature over the exact body before strict JSON parsing. A
durable Receipt provides replay, conflict, processing-lease, and retry behavior. Success
returns `applied`, `replayed`, or `ignored`; error bodies are stable and non-reflective.

The in-process, process-local authenticated-delivery rate limit defaults to 120 deliveries
per Connector per minute. Rate-limit and active-lease responses include `Retry-After`.

### `POST /v1/incidents/validate`

Validates without persistence. Returns canonical SHA-256 plus warnings.

### `POST /v1/incidents`

Validates, canonicalizes to protobuf, stores immutable content, and indexes the
incident transactionally.

- `201`: new artifact;
- `200`: exact same bundle ID and content (idempotent retry);
- `409`: same bundle ID with different canonical content;
- `413`: configured body limit exceeded;
- `415`: unsupported media type/encoding;
- `422`: structural/semantic/privacy invalidity;
- `503`: retryable storage failure.

### `POST /v1/capture`

Accepts one bounded `application/json` browser capture batch — the `CapturePayload`
the [`@earshot/browser`](../packages/browser/README.md) kernel drains — and turns it
into a governed Incident through the same WebRTC and audio-graph engines the SDK uses
(`framework: browser_capture`). It is a normal `/v1` route: the same bearer key or
viewer session authenticates it, the same project scoping applies, and because it is
an unsafe method a cookie-authenticated caller must send the CSRF token.

The wire format carries its own version in the body (`captureVersion`), independent of
the `/v1` path, so client and server evolve separately. The version is checked before
the rest of the schema, so a client on a format this server does not govern gets
`400 EARSHOT_UNSUPPORTED_CAPTURE_VERSION` rather than a list of field errors.

Every bound is explicit and enforced before the payload is materialized: a streamed
body limit, then per-collection count limits on `snapshots`, `deviceEvents`, `coverage`
and per-snapshot stats (`413 EARSHOT_CAPTURE_TOO_LARGE`), then the schema
(`422 EARSHOT_INVALID_CAPTURE`, field paths only, never payload values).

The client is not a trust boundary. The backend re-derives its own allowlist over every
`RTCStats` and device-event member and drops anything outside it before an engine sees
the value, so a `base64Certificate`, DTLS `fingerprint`, `usernameFragment`, candidate
address, or device label cannot be stored. Refusals are counted in the response
(`rejected_*`) and recorded on the Incident as `capture.*` coverage; the batch's own
coverage is recorded under a `browser.` prefix so a client claim can never overwrite a
server-derived note.

Browser timestamps are recorded in the declared browser `ClockDomain` at their raw
readings and are never rebased onto the server clock, so cross-clock latency stays
unavailable until a real `ClockRelation` is supplied.

- `201`: the batch became a new Incident;
- `200`: the same batch was already ingested (delivery is idempotent by batch content,
  so a transport retry after an unknown outcome does not duplicate evidence);
- `400`: malformed payload or unsupported `captureVersion`;
- `413`: body or collection limit exceeded;
- `415`: unsupported media type;
- `422`: payload fails the capture contract.

### `GET /v1/incidents`

Stable cursor pagination, optionally filtered by `session_id`. `limit` is 1–100.
Incidents denied for the `local_api` destination are removed in the indexed SQL query
before pagination, including from cursor material.

### `GET /v1/metrics/turns`

Returns project-scoped fleet summaries for STT finalization, EOU, first-token/first-audio,
send/receive/render response, overall response, or explicit native turn duration, grouped
by framework, provider, model, STT language, or status. Percentiles are stratified by
availability, basis, confidence, and limitation; unlike evidence is never blended. Missing
evidence is not converted to zero. The projection is rebuilt from canonical Incidents on
startup.

API `0.6.0` restricts the aggregation to `final` Incidents. A crash-recovered or
operator-sealed artifact is `provisional`: it covers an unknown fraction of its
conversation, so pooling its turns would move every percentile without saying why. The
exclusion is declared rather than performed quietly — `incident_count` is what the groups
cover, `withheld_incident_count` and `withheld_turn_count` are what they refused, and
`limitations` states what these numbers structurally cannot answer. Empty `groups` beside
a non-zero `withheld_incident_count` is a refusal to aggregate, never a measured zero.

### `GET /v1/incidents/{bundle_id}`

Content negotiation returns canonical protobuf or pretty debug JSON. A strong `ETag`
hashes the exact selected representation, `Vary: Accept` protects caches, and
`X-Earshot-Digest` identifies the canonical stored protobuf. Data responses use
`Cache-Control: no-store`. Every read verifies the canonical content digest and export
policy.

### `GET /v1/incidents/{bundle_id}/analysis`

Returns cached analysis for the exact artifact digest and analyzer version, or computes
and stores it separately. Export policy is checked before cached or newly computed
analysis is returned. Source evidence remains immutable.

The nested `analysis` value is a closed, metadata-only `DerivedAnalysis` contract.
Unknown projection fields, non-finite values, dangling operation/event/quality refs,
wrong session/digest/version/time bindings, and summaries inconsistent with source
counts are rejected before caching.

### `GET /v1/incidents/{bundle_id}/explanation`

Returns the versioned, backend-authored presentation projection: exact decimal-string
coordinates, true intervals only where comparable start/end evidence exists, point facts,
clock basis/domain, evidence IDs and provenance, governed stage measurements, coverage,
privacy omissions, finality/completeness, and analyzer limitations. The viewer positions
these facts but does not invent stage duration or cross-clock ordering.

The explanation response is a closed API contract. API `0.2.0` adds each event's explicit
`operation_id`, `trace_id`, and `span_id` when observed, plus an exact per-turn
`measurements` lane distinct from derived metrics. Operation-owned, turn-owned, and
ownerless measurement facts are mutually exclusive; repeated observations retain their
source values and provenance. API `0.3.0` adds a per-turn `interruption_chains` lane,
carried verbatim from the analyzer: one ordered causal chain per observed interruption
episode, each canonical stage marked observed (with its exact coordinate and cited
evidence) or not observed (with a coverage reason), plus a barge-in `effectiveness`
metric that is available only when both endpoints are observed and comparable. A turn
that observed no interruption carries an empty lane, which is an absence of evidence and
not a claim that none occurred. Validation checks exposed session, operation, event,
measurement, coverage, omission, diagnosis, ownership, and evidence fields independently
of the projection implementation. API and analyzer versions evolve independently. Pre-v1
clients pinned to API `0.1.x` must regenerate their response types before consuming
`0.2.x` explanations, and `0.2.x` clients must regenerate before consuming `0.3.x`.

### `GET /v1/incidents/{bundle_id}/contradictions`

Returns the evidence-linked contradictions detected in one incident's graph: reversed
same-domain operation intervals, duplicate and out-of-order transport deliveries, render
evidence that coverage says was never observed, and two observers disagreeing about one
turn quantity beyond their combined uncertainty. Each entry cites the real evidence IDs
it rests on and carries the boundary and turn it belongs to; no source payload is
surfaced. Detection is deterministic and source-order invariant.

The response names the `analyzer_version` and `input_digest` the detection ran against,
so an empty `contradictions` list means "examined, none found". When no analysis exists
for the incident the endpoint answers `404 EARSHOT_ANALYSIS_NOT_AVAILABLE` instead of an
empty list that would read as a clean bill of health. A stored analysis not derived from
this incident's evidence is refused with `409 EARSHOT_ANALYSIS_BINDING_MISMATCH`.

### `GET /v1/incidents/{bundle_id}/comparison`

Diffs an incident against a known-good incident named by the required
`known_good_bundle_id` query parameter, both resolved within the authenticated Project.
Reports diagnoses added and removed (by code, boundary, and turn), per-turn latency
deltas, availability changes, coverage gaps gained and lost, unmatched turns, and the
contradictions the incident has that the baseline does not.

A latency delta appears only where both sides are `available` in the same unit; every
other case is reported as an availability change rather than a fabricated number. Both
sides are pinned by the digest their analysis was derived from. The baseline keeps its
own error codes — `EARSHOT_KNOWN_GOOD_NOT_FOUND`, `EARSHOT_KNOWN_GOOD_PURGED`, and
`EARSHOT_KNOWN_GOOD_ANALYSIS_NOT_AVAILABLE` — so a caller always knows which of the two
incidents is unavailable.

### `GET /v1/incidents/{bundle_id}/export`

Projects one incident through a named exporter in the process-wide exporter registry
(`format`, default `otlp`; the generated OpenAPI enumerates the registry's names, and a
host process that registered its own exporter can select it here). Two policy gates run
before any document is produced: the `local_api` destination that governs reading the
incident out through this API at all, then the exporter's own declared destination,
enforced by the registry rather than by the route. A capture policy that forbids either
yields `403 EARSHOT_EXPORT_DENIED`; a name no exporter is registered under yields
`400 EARSHOT_UNKNOWN_EXPORT_FORMAT`. The response carries the projected `document`
alongside the `format`, the governed `destination`, and the artifact `digest`.

### `DELETE /v1/incidents/{bundle_id}`

Physically purges evidence and derived analysis, leaving a content-free tombstone.
Repeated purge is idempotent; retrieval returns 410.

## Live sessions

`/v1/live/*` is a separate collection from `/v1/incidents` because a conversation still
being written is a different kind of thing from an artifact. A live session is never
listed as an incident, never carries a digest, and therefore never carries analysis: a
`DerivedAnalysis` binds to `input_sha256`, and there is nothing to bind to yet. The
listing states that as a limitation rather than returning an empty analysis, because
"analysis did not run" and "analysis found nothing" are different claims.

Two sources feed the same buffer. `earshot serve --checkpoint-dir DIR` (env
`EARSHOT_CHECKPOINT_DIR`) follows the crash-recovery journals in a directory this
process can read; `POST …/checkpoints` accepts frames uploaded by a remote producer.
Following a directory is an explicit opt-in, the same storage decision as writing one.

### `GET /v1/live/sessions`

Project-scoped list of open journals: identity, `state`
(`live` / `stale` / `finalized` / `abandoned`), the journal sequence reached, whether a
close was observed, whether the journal is complete, and whether an operator could seal
it. The response also carries the collection's own `limitations`.

### `GET /v1/live/sessions/{session_id}/tail`

`text/event-stream`. Server-sent events, not a WebSocket: every guarantee this backend
makes — unsafe-binding refusal, the loopback `Host` check, bearer / API-key /
browser-session authentication, CSRF, project scoping — lives in one
`@app.middleware("http")`, and Starlette does not run HTTP middleware for WebSocket
scopes. As an ordinary `GET`, the tail inherits that stack unchanged, is covered by the
same-origin policy (the API sets no CORS headers), and gets `Last-Event-ID` resume for
free. A live request carrying an `Origin` that is not this host is refused with
`403 EARSHOT_ORIGIN_NOT_ALLOWED` unless it authenticated with a bearer token.

Events are the journal's own record kinds, verbatim and without inference: `open`,
`record`, `withheld`, `operation_open`, `limit`, `exhausted`, `finalize`, plus the control
events `replay_truncated`, `reset`, `overflow`, `end`, and a periodic `heartbeat`. Every
record-bearing event carries `id: <journal_id>:<sequence>`; control events deliberately
carry no `id`, so they can never advance a client's resume cursor past a position it did
not receive.

A subscriber is outside the recording process, so the tail is a restricted export and
reapplies its destination policy exactly as the exporter registry does at its own seam.
Its destination name is `live_tail`. The `open` event's `export_policy` declares that
name, whether the policy could be read at all, and which enabled capture classes forbid
it. Once the session has actually retained such a class — the same _captured_ keying a
finished bundle is governed by — the content stops: that record arrives as a `withheld`
event at its own sequence, carrying the structural entry kind, the destination, and each
class that refused with its reason (`export_denied_by_policy` or
`export_destination_not_permitted`), and nothing of the record itself. Absence is
declared rather than silent, because a stream that simply skipped the record would read
as a session that never said anything. `limit`, `exhausted` and `finalize` keep flowing:
they carry counters, reasons and status, never content. The check is fail-closed — a
policy the server cannot rebuild, or a capture class this build cannot name, withholds
everything and says `export_policy_unreadable`.

What cannot be known mid-session is said on the wire rather than left absent. The `open`
event carries `in_progress: true` and `unknown_until_close`, which enumerates session
status and end, manifest finality and completeness, the privacy manifest, turn
membership, turn metrics, interruption classification, derived analysis, and diagnoses.
An operation that started and has not been observed to end arrives as its own
`operation_open` event with `status: "unknown"`, `ended_at: null`, `duration_nano: null`
and `end_observed: false`, so a client physically cannot render it as a completed
`Operation`.

`from=start` (default) replays the retained window, `from=live` sends only what arrives
next, and `from=<sequence>` resumes at a position. `Last-Event-ID` overrides all three;
when it names a different journal the server emits `reset` first, so two sessions cannot
be spliced into one client-side timeline. Anything the replay window no longer holds is
declared with `replay_truncated` rather than silently skipped.

Backpressure is lossless by construction. Every buffer is bounded, and a subscriber that
falls behind its per-connection queue receives `overflow` and has its stream closed
rather than having events dropped: the durable journal still holds every record, so a
reconnect with `Last-Event-ID` catches up exactly. Over-capacity connections are refused
with `429 EARSHOT_TAIL_CAPACITY`, and an unknown or out-of-project session is
`404 EARSHOT_SESSION_NOT_LIVE`.

### `POST /v1/live/sessions/{session_id}/checkpoints`

`Content-Type: application/vnd.earshot.checkpoint+frames`, a contiguous run of plaintext
journal frames. Separator, length bound, CRC and strict sequence contiguity are checked
exactly as the journal reader checks them. A batch with a torn tail is refused whole
(`400 EARSHOT_CHECKPOINT_FRAMES_INVALID`) — a torn tail is meaningful at the end of a
crashed file, but in an upload it only means a malformed request, and accepting a prefix
would let a client decide where the server's evidence stops. A batch that skips a
sequence is `409 EARSHOT_CHECKPOINT_SEQUENCE_GAP`. Per-project session quotas return
`429 EARSHOT_LIVE_CAPACITY`. An encrypted journal cannot be uploaded: the server holds
no key, so its header does not decode.

A live session is named by `(project, session_id)`, never by the session id alone. A
session id is a producer's own name for its own call, so two projects may each have a
`call-1` and neither can take the other's: whichever project uploaded first would
otherwise own the name and make every later upload from the other a permanent `404`. A
session id another project holds is answered exactly as an id nobody holds is, so
existence never leaks across tenants.

An upload may extend the journal and may repeat it; it may never edit it. Re-sending
frames already accepted is idempotent and republishes nothing — the uploader restarts at
offset zero after a process restart, so a full replay is ordinary. Re-sending a sequence
with _different_ content is `409 EARSHOT_CHECKPOINT_DIVERGED`, compared against the
CRC-32 each frame already carries, and a sequence the server cannot verify is refused the
same way rather than accepted on trust. Any frame after the journal's `finalize` — in a
later batch or after a `finalize` in the same batch — is
`409 EARSHOT_CHECKPOINT_JOURNAL_FINALIZED`: the recorder closed, so a later frame is a
different journal wearing this one's name. Every refusal leaves the session exactly as it
was, because the whole batch is judged before any of it is recorded.

One frame may be at most 1 MiB, which is also the largest batch and the body limit of
this endpoint (`413 EARSHOT_BODY_TOO_LARGE` beyond it). That single number is
`earshot.checkpoint.limits.MAX_CHECKPOINT_FRAME_BYTES`, and the uploader, the registry's
frame scan and this endpoint all read it from there. The local journal frames records up
to 32 MiB and keeps doing so — a transport bound must not damage the durable record — so
a session whose journal frames a larger record (a raw OTLP passthrough is the one record
kind that reaches this size, and the tail withholds its payload anyway) stops being
followed live at that sequence. The uploader says which sequence and stops; the listing
declares the bound in `limitations`; the complete session still travels through
`POST /v1/incidents` or an operator seal.

### `POST /v1/live/sessions/{session_id}/seal`

The only path from a live buffer to an artifact, and always an operator action. The
server never seals on its own: it cannot distinguish a crashed producer from a slow one,
and guessing would manufacture an artifact nobody produced. Sealing a journal that
reached close reproduces exactly what the producer will send, so it keeps its bundle id
and content-addressed ingest deduplicates it. Sealing one that did not produces a
_provisional_ artifact — `finality: "provisional"`, `completeness: "incomplete"`,
`session.status: "interrupted"`, no session end, and a `manifest.recovery` declaration —
under a distinct, deterministic bundle id derived from the sequence sealed, so the
producer's own final artifact can still land. A session that outgrew its retained frame
window is `409 EARSHOT_SESSION_NOT_SEALABLE` rather than being sealed short.

Live buffers expire on a TTL and are dropped as soon as the real artifact is ingested
through `POST /v1/incidents`.

## Strict request handling

The server reads request bytes directly to enforce:

- declared and streamed body size;
- UTF-8 and strict JSON (`NaN`/`Infinity` rejected);
- duplicate object-key rejection;
- configured maximum nesting depth for both JSON input and protobuf's embedded
  canonical JSON;
- controlled codec errors that do not reflect payload values; and
- full validation before any database mutation.

No request handler dereferences media locators.

After streaming the bounded request body, structural decode, JCS/protobuf work, and
durable storage are offloaded from the ASGI event loop. A blocked SQLite/CAS write
therefore does not block `/healthz`.

## Storage layout

```text
.earshot/
  earshot.sqlite3
  instance-correlation.key
  objects/sha256/ab/<remaining digest>
  tmp/
```

SQLite uses foreign keys, secure deletion, WAL, a busy timeout, full synchronous
writes, and one write transaction per ingest.
Object files are written to a temporary file, flushed, fsynced, and atomically linked
into the content-addressed directory. Corruption is explicit, never silently repaired.

## Run

```bash
earshot serve --data-dir .earshot
EARSHOT_TOKEN=... earshot serve --host 127.0.0.1 --behind-tls-proxy
earshot serve --data-dir .earshot --checkpoint-dir .earshot/journals
```

An event stream wants a direct connection or an SSE-aware proxy. The tail sends
`Cache-Control: no-store` and `X-Accel-Buffering: no` and heartbeats every 15 seconds; a
buffering proxy will still turn it into a long poll.

Uvicorn access logging is off by default so bundle IDs do not enter a second retention
domain.
