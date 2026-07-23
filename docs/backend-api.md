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
source values and provenance. Validation checks exposed session, operation, event,
measurement, coverage, omission, diagnosis, ownership, and evidence fields independently
of the projection implementation. API and analyzer versions evolve independently. Pre-v1
clients pinned to API `0.1.x` must regenerate their response types before consuming
`0.2.x` explanations.

### `DELETE /v1/incidents/{bundle_id}`

Physically purges evidence and derived analysis, leaving a content-free tombstone.
Repeated purge is idempotent; retrieval returns 410.

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
```

Uvicorn access logging is off by default so bundle IDs do not enter a second retention
domain.
