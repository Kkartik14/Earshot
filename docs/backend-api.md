# Backend API

Default address: `http://127.0.0.1:4319`. Port 4318 is intentionally left available
for a standard OTLP/HTTP receiver.

The reproducible machine contract is
[`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json). Incident request
bodies reference the generated incident schema; analysis responses reference
[`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json).

Tokenless development is loopback-only and rejects non-loopback `Host` headers. The
default `operator` mode supports project API keys, expiring server-created viewer
sessions, and the legacy default-project bearer token. API keys are exchanged once for
an HttpOnly, SameSite=Strict viewer cookie; unsafe cookie-authenticated methods require
the in-memory CSRF token. Every repository call is scoped from its authenticated
principal, and an incident in another project is indistinguishable from a missing
incident. The request middleware checks the actual ASGI listener and refuses both
`/v1/*` and `/hooks/v1/*` on an unexpected non-loopback plaintext bind.

Hosted deployments use the separate `hosted_jwt` mode. Configure
`EARSHOT_AUTH_MODE=hosted_jwt`, `EARSHOT_JWT_ISSUER`, `EARSHOT_JWT_AUDIENCE`, and an
HTTPS `EARSHOT_JWKS_URL`; `EARSHOT_JWKS_CA_FILE` optionally supplies a private CA.
Hosted mode rejects `EARSHOT_TOKEN` and all operator API-key or browser-cookie
fallbacks. The receiver verifies RS256 or ES256, `kid`, signature, exact issuer,
one-string audience, `sub`, `project_id`, scope, `iat`, `exp`, and `jti`. Tokens may
live for at most five minutes. Every route project ID must equal the signed claim;
`X-Earshot-Project-Id`, when supplied, is only a matching assertion. The issuer, JWKS,
audience, and scope vocabulary are deployment configuration. The route scopes listed
below are Earshot's current contract proposal and must be agreed with the Platform token
issuer before hosted acceptance.
JWKS keys are cached for five minutes. An unknown key ID triggers at most one refresh
per 30 seconds per process, so signing-key rotation needs an overlap window while
instances refresh their cache.

Hosted `/v1/auth/*` cookie exchange is disabled. Keep operator auth and hosted JWT auth
as distinct deployment modes; do not mix their credentials. Health and readiness remain
unauthenticated process checks and do not return project data.

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

Bundle identifiers occupy one installation-wide namespace. Standalone/operator ingest
supplies its own collision-resistant ID. Hosted ingest omits the ID and lets Earshot
mint one from the signed project and the stable `Idempotency-Key`; producer identifiers
are not used as Earshot bundle IDs. Projects are single-organization authorization
scopes in this alpha, not hostile SaaS tenant boundaries.

Hosted JSON uses a closed metadata profile: bounded runtime-family slug and
semantic release version, the
host-assigned session ID, categorical session status and timestamps, bounded event
IDs/types/timestamps, and an optional opaque runtime-session ID. Earshot creates the
metadata-only privacy policy. Hosted requests reject free-form maps, unknown fields,
operations, media, and raw OTLP; operator JSON and protobuf retain the general Incident
contract.

Hosted `/v1/incidents` writes also require all three finite per-environment allowlists:
`EARSHOT_HOSTED_RUNTIME_NAMES`, `EARSHOT_HOSTED_SESSION_STATUSES`, and
`EARSHOT_HOSTED_EVENT_NAMES`. Each is a comma-separated list. Their empty defaults keep
hosted runtime-event ingestion closed with `409 EARSHOT_HOSTED_CONTRACT_NOT_ACCEPTED`
until Platform and TVIC accept the vocabulary. Runtime versions use numeric
`MAJOR.MINOR.PATCH` form. Browser capture has separate finite coverage and resync
vocabularies; hosted capture IDs are replaced with stable opaque values before evidence
is stored. The `capture-` session ID namespace is reserved for Earshot's continuous
browser-call identity; hosted runtime artifacts using that prefix are rejected to keep
an imported artifact from replacing a live capture journal.

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

### `POST /v1/auth/session`

Exchanges a valid bearer credential (project API key or the legacy token) for an
expiring, HttpOnly, SameSite=Strict viewer session cookie plus an in-memory CSRF
token, revoking any prior session cookie on the same request first. `401
EARSHOT_UNAUTHORIZED` if the request did not authenticate by bearer credential.

### `GET /v1/auth/session`

Reports current viewer session status: `authenticated`, `authentication_required`,
`project_id`, `csrf_token`, `expires_in_seconds`. Under `--trust-local-network` with
no project key configured, answers `authenticated: false` /
`authentication_required: false` instead of requiring a login, so the bundled
viewer still loads.

### `POST /v1/auth/logout`

Revokes the caller's viewer session and clears the cookie. Requires an active
session (not a bearer credential) plus the CSRF token; `401 EARSHOT_UNAUTHORIZED`
otherwise.

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
incident transactionally. In operator mode, the request contains `bundle_id` and an
optional `Idempotency-Key` must match it. In hosted JWT mode, send JSON, omit
`profile.manifest.bundle_id` (the hosted schema does not accept this field), and provide
an opaque stable `Idempotency-Key` for the immutable artifact submission. Earshot mints
the bundle ID using its durable instance
correlation key, scoped to the signed project. The host must persist the returned ID in
its call/session mapping. Retries with the same key and canonical content return the
same ID; a changed body under the same key returns `409`. A later immutable snapshot
uses a new key. The correlation key is part of the Earshot backup set.

- `201`: new artifact;
- `200`: exact same bundle ID and content (idempotent retry);
- `409`: same bundle ID/key with different canonical content;
- `400`: hosted ID/key rules are violated;
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
Continuous `captureVersion: 2` calls also have a cumulative durable-journal byte cap
(64 MiB by default). A drain that would exceed it returns `413
EARSHOT_CAPTURE_JOURNAL_LIMIT`; the earlier committed journal prefix stays intact
and visible through the live-session surface. Retrying the same oversized body
repeats the refusal. A smaller replacement body may use the same unapplied drain
sequence; after it is accepted, the earlier oversized body conflicts with that
resolved sequence.

Continuous drains are ordered by `drainSequence`. A skipped sequence returns
`409 EARSHOT_CAPTURE_SEQUENCE_GAP` unless the next drain declares a `resync` range
ending at `drainSequence - 1`; the server records only the missing range as
`capture.drain_sequence` coverage and invalidates the WebRTC carry across it. A
request may have committed even when its response was lost, so if the resync range
overlaps an already-applied prefix the server clips that prefix using its durable
sequence and counts only the still-missing suffix. Hosted capture accepts the
finite reasons `client_buffer_overflow` and `upload_failed_payload_dropped`.

Two coherence rules follow the schema, because a payload that contradicts itself
cannot be turned into evidence without guessing. A `traceparent` that disagrees with
the `traceId`/`spanId` sent beside it is `422 EARSHOT_INCOHERENT_TRACE_CONTEXT` rather
than a silent choice of one spelling. A batch whose `timestamp_ms` readings move
backwards is `422 EARSHOT_CAPTURE_NON_MONOTONIC`: normalizing it would place an
observation at a coordinate the browser never reported and difference the cumulative
`getStats` counters over a negative interval.

The batch's trace context, when it sends one, is recorded on the facts themselves
(`trace_id`/`span_id`), so correlating a capture with the application's trace is a
property of the stored Incident and not only of the response. Facts derived from the
batch are attributed to the browser that observed them (`evidence.observer: browser`),
matching the browser `ClockDomain`'s own declaration, and a client-reported
`coverage[].droppedCount` is retained as `dropped_count` on the coverage note.

The client is not a trust boundary. The backend re-derives its own allowlist over every
`RTCStats` and device-event member and drops anything outside it before an engine sees
the value, so a `base64Certificate`, DTLS `fingerprint`, `usernameFragment`, candidate
address, or device label cannot be stored. Refusals are counted in the response
(`rejected_*`) and recorded on the Incident as `capture.*` coverage; the batch's own
coverage is recorded under a `browser.` prefix so a client claim can never overwrite a
server-derived note.

Set `EARSHOT_CAPTURE_JOURNAL_DIR` to persist in-flight call journals and drain retry
identity across a process restart. Provision the directory before starting Earshot and
mount it on durable storage; Earshot does not create it. Durable capture requires POSIX
process file locking. Earshot holds an exclusive lock for that directory and refuses a
second writer. Hosted `captureVersion: 2` requests return `503` with `Retry-After` when
the journal is absent or unavailable. Hosted retry ledgers also carry a non-secret
fingerprint of the store's correlation key; a missing or replaced key fences hosted
continuous capture until the matching backup is restored or the affected project is
deleted. Operator-mode capture can remain in memory when the directory is unset.
Live-session state and SSE fan-out are still process-local.

Durable capture is bounded to `LiveConfig.max_sessions` calls server-wide (32 by
default) and `LiveConfig.max_sessions_per_project` per project (16 by default). A
completed call releases its durable slot after it is sealed. A `429
EARSHOT_CAPTURE_CALL_CAPACITY` means one of those limits is full; resume an existing
call, seal a completed call, or have the project deletion coordinator erase the
project. Unfinalized durable calls continue to count after expiry or restart because
their ownership and retry state remain on disk.

The server keeps exact replay digests for up to 1,024 sealed capture calls by default
(`ApiConfig.max_sealed_capture_replay_ledgers`). A ledger used by an active retry stays
available until that drain completes. Once an older replay ledger is pruned, the
immutable artifact store still reserves its bundle ID. A delayed retry for that ID
returns `409 EARSHOT_CAPTURE_REPLAY_EXPIRED` and cannot recreate the call.

Hosted live checkpoint frames may be accepted with `earshot:write`, but sealing a
generic hosted checkpoint returns `409 EARSHOT_HOSTED_CONTRACT_NOT_ACCEPTED` until the
runtime artifact contract is accepted. Hosted browser capture uses its separate,
metadata-only contract and can be sealed; a successful seal of a finalized capture
also releases its durable slot.

Browser timestamps are recorded in the declared browser `ClockDomain` at their raw
readings and are never rebased onto the server clock, so cross-clock latency stays
unavailable until a real `ClockRelation` is supplied.

- `201`: the batch became a new Incident;
- `200`: the same batch was already ingested (delivery is idempotent by batch content,
  so a transport retry after an unknown outcome does not duplicate evidence);
- `400`: malformed payload or unsupported `captureVersion`;
- `413`: body or collection limit exceeded, or a continuous call would exceed its
  cumulative durable-journal byte limit;
- `429`: continuous-call capacity is full for the project or server;
- `415`: unsupported media type;
- `409`: a call ID whose sealed replay ledger has expired is already reserved by an
  artifact or deletion tombstone;
- `422`: payload fails the capture contract, contradicts its own trace context, or
  carries readings that move backwards.
- `503`: hosted continuous capture has no usable durable journal, or an existing
  journal exceeds the configured recovery byte cap; retry after the indicated delay
  or after the configuration or journal state is repaired.

### `GET /v1/incidents`

Stable cursor pagination, optionally filtered by `session_id`. `limit` is 1–100.
This metadata-only projection reads from the SQLite catalog; it does not load or decode
artifact bytes. Incidents denied for the `local_api` destination and expired incidents
are removed in the indexed SQL query before pagination, including from cursor material.

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
`Cache-Control: no-store`. Artifact reads verify the canonical content digest and
enforce the stored export policy.

### Incident related-record references

`GET /v1/incidents/{bundle_id}/references` lists project-scoped links to records
owned by other services. `PUT /v1/incidents/{bundle_id}/references/{namespace}/{record_type}`
upserts one link from `{ "external_id": "..." }`; repeating the same value is
idempotent and preserves its original link time. Replacing it updates that time.
`DELETE` on the same path removes the link and returns `204`. Cookie-authenticated
`PUT` and `DELETE` requests require the viewer CSRF header.

The route uses the authenticated project's scope. A bundle owned by another project
returns the same not-found response as an ordinary incident read. Namespace and record
type are lower-case portable identifiers; external IDs are 1–256 ASCII letters,
digits, period, underscore, tilde, colon, or hyphen. URLs and evidence payloads do not
belong in this table. References are mutable catalog metadata and do not alter the
canonical bytes, digest, or derived analysis. An authorized project may delete a
reference even when its incident's export policy denies evidence reads. Purging or
retention expiry removes the references with the incident.

### Project summary

`GET /v1/projects/{project_id}/summary` returns up to 50 retained sessions with
`session_id`, `status`, `framework`, `framework_truncated`, and
`created_at_unix_nano`. Framework names are capped at 128 characters; the boolean
marks legacy values that were shortened. It returns no host links, artifact bytes,
transcript, analysis, or references. Its hosted scope is `earshot:summary:read`.

The optional Platform adapter is off by default. Set
`EARSHOT_PLATFORM_ADAPTER_ENABLED=true` (or `earshot serve --platform-adapter`) only
with `EARSHOT_AUTH_MODE=hosted_jwt`. It exposes
`GET /v1/platform/projects/{project_id}/observe/summary` and adapts the generic
projection to Platform's `{summary, items}` response. A canonical lowercase Platform
UUID is the Earshot project ID as well; there is no `platform_...` mapping. The route
requires the JWT project claim to match its path. `x-platform-project-id`, when sent,
is an assertion and never grants access. It returns up to 50 metadata-only rows with a
same-origin `/observe?sessionId=...` link. Its hosted scope is also
`earshot:summary:read`. The adapter lives under
[`earshot.integrations.platform`](../packages/sdk-python/src/earshot/integrations/platform/adapter.py)
and adds no Platform dependency to the generic summary service.

### Hosted scope proposal

In `hosted_jwt` mode, every project-data route requires the scope shown here. This
literal vocabulary is implemented by Earshot but still needs token-issuer acceptance.

| Operation                                                                  | Required scope            |
| -------------------------------------------------------------------------- | ------------------------- |
| List/read incidents, references, analyses, exports, live sessions, and SSE | `earshot:read`            |
| Ingest incidents/captures/checkpoints and write references                 | `earshot:write`           |
| Delete a reference                                                         | `earshot:delete`          |
| Delete an immutable incident artifact                                      | `earshot:artifact:delete` |
| Read either summary endpoint                                               | `earshot:summary:read`    |
| Delete a project                                                           | `earshot:project:delete`  |

### Hosted detail and live APIs

The BFF obtains the user and project from its authenticated Platform session, mints a
short-lived Earshot-audience token, and proxies only the routes authorized for that
project. Earshot detail is `GET /v1/incidents/{bundle_id}`; analysis and explanation
are separate `GET` routes below that artifact. `GET /v1/live/sessions` lists active
sessions, and `GET /v1/live/sessions/{session_id}/tail` is an SSE stream. The BFF must
forward `Last-Event-ID`, preserve the event stream without buffering, and cancel the
upstream request when the browser disconnects. These routes all check the signed
project and use `earshot:read`.

Live producers can submit bounded checkpoint frames with
`POST /v1/live/sessions/{session_id}/checkpoints` using `earshot:write`. This accepts
Earshot's checkpoint framing contract; it is not a generic TVIC webhook or permission
for the TVIC SDK to call Earshot directly. A stable session ID is project-scoped, and
cross-project lookup remains indistinguishable from a missing session.

The viewer package makes same-origin API requests and uses `EventSource` for SSE. A
host must proxy both through its authenticated BFF; no service JWT belongs in browser
JavaScript. Mount `ObserveFeature` inside one shared React Query client and provide
`ViewerQueryScopeProvider` with the current `projectId` and an opaque `authContextId`.
Change the auth context whenever the effective grant changes, including a user change
within the same project. Query keys and live stores are scoped to both values, and
cleanup removes only Earshot-owned cache entries.

### Project deletion

`DELETE /v1/projects/{project_id}` is available only in hosted JWT mode. It requires
`earshot:project:delete` and an exact match with the signed project claim. It durably changes the project from `active` to
`deleting` before removing data, which fences new writes and retries. The built-in
`default` project cannot be deleted. Project deletion removes Earshot incidents,
external references, analyses and projections, connector/retry receipts, connectors,
API keys, capture-call journals, and queued CAS objects attributable to those
incidents. An unattributed CAS orphan is preserved for explicit maintenance rather
than guessed to belong to the deleting project. A hashed bundle-ID tombstone remains
to prevent a purged bundle ID from being reused; the project remains as a `deleted`
lifecycle record with no display name.

`200 {"state":"deleted"}` means the Earshot-owned cleanup completed. `202
{"state":"deleting","retry_after_seconds":3}` with `Retry-After: 3` means writes
are fenced but cleanup is pending. Each request removes at most 500 selected incident
rows. After incidents are gone, it can remove up to 500 rows from each auxiliary table
in order, plus up to 500 queued CAS objects. Incident deletion can also cascade graph,
analysis, and reference rows. Cleanup returns pending while work remains.
SQLite page compaction runs after releasing the application mutation lock; when a WAL
checkpoint is busy, the project stays fenced and the response remains pending. Repeat
the same project-scoped DELETE until it returns 200. Storage or file removal failures remain pending; the caller must keep the
host's overall deletion state incomplete. Earshot does not remove producer-owned
checkpoint source files, backups, snapshots, or other services' records. Their owners
must acknowledge their own deletion before Platform reports project erasure complete.
Retention deadlines are expiries, not minimum-storage holds; explicit erasure overrides
future expiry. Legal holds are not represented by the current API.

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

### `GET /v1/incidents/{bundle_id}/evidence/summary`

Returns `EvidenceQuery(bundle, analysis).summary()`: a metadata-only, evidence-cited
digest of what is known about the incident's turns — first abnormal boundary,
recomputability, and a compact per-turn rollup. Built inside the same projection
wrapper as analysis/explanation, so an analysis derived from other evidence surfaces
as `409 EARSHOT_ANALYSIS_BINDING_MISMATCH` rather than a 500.

### `GET /v1/incidents/{bundle_id}/evidence/not_observed`

Returns `EvidenceQuery(bundle, analysis).not_observed()`: the coverage/omission facts
that were explicitly not captured or not exposed, cited by boundary and turn. Absence
here is a stated reason, never a fabricated zero. Same binding-mismatch behavior as
`evidence/summary`.

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

The path from a live buffer to an artifact, invoked by an authorized caller. The
server never seals on its own: it cannot distinguish a crashed producer from a slow one,
and guessing would manufacture an artifact nobody produced. Hosted callers can seal
metadata-only browser-capture sessions; generic hosted checkpoint sealing stays gated
until the runtime artifact contract is accepted. Sealing a journal that reached close
reproduces exactly what the producer will send, so it keeps its bundle id and
content-addressed ingest deduplicates it. Sealing one that did not produces a
_provisional_ artifact — `finality: "provisional"`, `completeness: "incomplete"`,
`session.status: "interrupted"`, no session end, and a `manifest.recovery` declaration —
under a distinct, deterministic bundle id derived from the sequence sealed, so the
producer's own final artifact can still land. A session that outgrew its retained frame
window is `409 EARSHOT_SESSION_NOT_SEALABLE` rather than being sealed short.

Live buffers expire on a TTL and are dropped as soon as the real artifact is ingested
through `POST /v1/incidents`.

Sealing a finalized capture can succeed even if its HTTP response is lost. The
successful seal stores the final incident and drops the live buffer, so a repeated
seal can then return `404 EARSHOT_SESSION_NOT_LIVE`. A host with a durable seal
outbox should reconcile an ambiguous result by querying both
`GET /v1/incidents?session_id=<call_id>` and `GET /v1/live/sessions` in the same
project. A matching `finality: "final"` artifact alone does not complete the
outbox: Earshot can ingest the artifact and then fail to persist the durable
capture replay fence, returning `503 EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE` while
the finalized live session and its capacity reservation remain. If the call is
still in the live list, retry the same idempotent seal even when the final
artifact already exists. Complete the outbox only when the matching final
artifact exists and the call is absent from the live list. If neither is
visible, or either reconciliation read is unavailable, keep the item pending
for repair rather than treating 404 as proof that the seal failed.

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
