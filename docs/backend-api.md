# Backend API

Default address: `http://127.0.0.1:4319`. Port 4318 is intentionally left available
for a standard OTLP/HTTP receiver.

The reproducible machine contract is
[`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json). Incident request
bodies reference the generated incident schema; analysis responses reference
[`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json).

Tokenless development is loopback-only and rejects non-loopback `Host` headers. Remote
deployments must explicitly declare a trusted TLS proxy and authenticate `/v1/*` with a
project API key or the legacy default-project bearer token. Every repository call is
scoped from that principal; an unknown incident in another Project is indistinguishable
from a missing incident. The request middleware checks the actual ASGI listener as well
as declared configuration and refuses both `/v1/*` and `/hooks/v1/*` on an unexpected
non-loopback plaintext bind.

Bundle identifiers occupy one installation-wide namespace. Producers should use UUIDv4,
UUIDv7, or Connector-generated collision-resistant IDs. Projects are single-organization
authorization scopes in v1, not hostile SaaS tenant boundaries.

Provider `/hooks/*` routes are a separate trust boundary. They do not accept Earshot
bearer credentials as provider proof and do not return Project identifiers.

## Media types

```text
application/vnd.earshot.incident+protobuf
application/vnd.earshot.incident+json
```

`application/x-protobuf` and `application/json` are accepted aliases. Compressed
request bodies are not accepted in v1.

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
by framework, provider, model, or status. Percentiles are stratified by availability,
basis, confidence, and limitation; unlike evidence is never blended. Missing evidence is
not converted to zero. The projection is rebuilt from canonical Incidents on startup.

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
