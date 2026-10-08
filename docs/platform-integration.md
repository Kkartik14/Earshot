# Platform and TVIC integration

Earshot owns evidence ingestion, retention, analysis, and the Observe feature.
Platform owns accounts, user/project authorization, the authenticated product
shell, and the deletion coordinator. TVIC owns runtime execution and runtime
session state. Voice Labs owns experiment and evaluator records. Each service
stores and deletes its own data; stable cross-service IDs are references, not a
shared database schema.

## Ingest boundary

The finalized-artifact API is `POST /v1/incidents`, using a closed hosted metadata
profile. It includes bounded runtime name/version, the host-assigned `session_id`,
categorical session status and available times, and events with stable IDs,
machine-readable types, and available times. The optional `runtime_session_id` is
opaque correlation metadata. The API rejects attributes, operations, media references,
raw OTLP, and unknown fields; Earshot creates its own metadata-only privacy policy.
The runtime name is a lowercase runtime-family slug and the version is a semantic
release version; the worker does not map provider or model names into those fields.
The host sends a stable opaque
`Idempotency-Key` for each immutable artifact submission and omits
`profile.manifest.bundle_id`; Earshot assigns and returns the bundle ID. The ID is
scoped to the signed project and minted from Earshot's durable instance correlation
key, so the host does not choose or reveal it. Exact retries with the same key and
canonical content resolve to the same artifact. Reusing a key with changed content
returns `409`; a later snapshot uses a new key. The Platform worker persists the
returned Earshot bundle ID alongside its existing call/session mapping.

`session_id` names the observed runtime session and is assigned by the Platform host.
Do not use the reserved `capture-` prefix; Earshot uses it for continuous browser-call
IDs and rejects `/v1/incidents` writes in that namespace in every auth mode.
Earshot stores the optional TVIC ID as the bounded `session.id` profile correlation
attribute. The host also assigns durable event IDs and preserves source timestamps only
when the runtime surface supplies them. Consumers do not infer identity or timing from
labels or approximate timestamps. Standalone/operator ingest retains the existing
JSON/protobuf contract where the caller supplies its bundle ID.

For live viewing, an authorized host can submit Earshot checkpoint frames to
`POST /v1/live/sessions/{session_id}/checkpoints`. This is Earshot's bounded
checkpoint framing protocol, not an unrestricted runtime-event endpoint.
`POST /v1/capture` is the separate browser WebRTC/device capture API. TVIC must
not import Earshot or post directly to it. Platform's host-side worker forwards
only events accepted by the runtime event contract proposal below.

## Proposed TVIC-to-Earshot event contract

The versioned metadata-only proposal is in
[`contracts/runtime-event-v1.md`](contracts/runtime-event-v1.md). It specifies
host-owned project/call/session/event retry identity, available source and
observation times, and fields excluded by default. The proposal is not yet
accepted by the TVIC or Platform service owners, and this checkout does not
contain a TVIC TypeScript-to-incident producer or runtime event adapter. A real
TVIC-to-Earshot acceptance call therefore remains pending cross-team contract
and producer work.

Set `EARSHOT_HOSTED_RUNTIME_NAMES`, `EARSHOT_HOSTED_SESSION_STATUSES`, and
`EARSHOT_HOSTED_EVENT_NAMES` to comma-separated values only after those runtime
vocabularies are accepted. Empty values leave hosted `/v1/incidents` writes closed.
Hosted live checkpoint frames can be written with `earshot:write`, but sealing those
generic frames remains unavailable until the artifact contract is accepted. The
separate metadata-only browser capture contract can be sealed; seal finalized calls to
release their durable capture slots.

For hosted `captureVersion: 2`, provision `EARSHOT_CAPTURE_JOURNAL_DIR` before
starting Earshot and mount it on durable storage. Run one ASGI writer against the
directory. Earshot does not create the directory; it requires POSIX process file
locking and holds an exclusive lock. Missing or unavailable journals return
`503` with `Retry-After`. Keep the capture directory and Earshot's
`instance-correlation.key` in the same backup set. A lost or replaced key prevents
hosted retry identity from being proven, so continuous capture stays fenced until
the matching backup is restored or project deletion completes. Live-session state
and SSE fan-out remain process-local. Durable slots are finite: seal completed calls;
unfinalized calls keep their slots while their retry state is retained. A capacity
`429` requires resuming an existing call, sealing a completed call, or completing
project deletion.

## Call and Lab references

References live in Earshot's project-scoped catalog, outside immutable
artifact bytes and digest-bound analysis. Each key is `(namespace,
record_type)` and stores one opaque external ID. Repeating the same value is
idempotent; replacing it updates the link time. A composite project/bundle
foreign key prevents cross-project references, and incident purge or retention
expiry removes the links.

After ingest, an authorized service can create a Platform call link:

```http
PUT /v1/incidents/{bundle_id}/references/platform/call
Content-Type: application/json

{"external_id":"call_01J..."}
```

Voice Labs can add its run link after evaluation:

```http
PUT /v1/incidents/{bundle_id}/references/voice_labs/run
Content-Type: application/json

{"external_id":"run_01J..."}
```

`GET /v1/incidents/{bundle_id}/references` lists links;
`DELETE /v1/incidents/{bundle_id}/references/{namespace}/{record_type}` removes
one. IDs are opaque, bounded strings; URLs and evidence content do not belong
in this table. Platform owns call/session mapping, Voice Labs owns run IDs, and
Earshot does not infer relationships from names, phone numbers, or timestamps.

## Hosted authorization

Standalone deployments keep `operator` auth mode: project API keys, optional
operator bearer token, and same-origin viewer sessions. Hosted deployments use
the adopted short-lived JWT contract in Platform Decision 0002.
Earshot verifies RS256 or ES256 signature and `kid` from configured HTTPS
JWKS, exact issuer, exact service audience, subject, signed project ID, scope,
integer `iat`/`exp`, and token ID. Expiry must be no more than five minutes
after issuance. Platform owns signing keys, user membership, and token minting.
Use each environment's exact configured issuer and Earshot audience. The five-minute
expiry is a hard maximum; Platform may mint two-minute service tokens when its refresh
behavior supports that lifetime. Earshot validates tokens and does not mint or refresh
them.

Set `EARSHOT_AUTH_MODE=hosted_jwt`, `EARSHOT_JWT_ISSUER`,
`EARSHOT_JWT_AUDIENCE`, and `EARSHOT_JWKS_URL`. Optionally set
`EARSHOT_JWKS_CA_FILE` for a private CA. Do not set `EARSHOT_TOKEN` in this
mode: hosted JWT and operator credentials are separate configurations, and
`/v1/auth/*` browser-cookie exchange is disabled for hosted JWTs. JWKS
unavailability is a retryable service error; invalid claims/signatures are
rejected. Health and readiness routes remain public process checks and expose
no project data.
JWKS keys are cached for five minutes, and an unknown key ID triggers at most one
refresh per 30 seconds per Earshot process. Keep old and new signing keys available
during rotation so instances can refresh before tokens rely on the new key.

Every resource path must match the verified `project_id` claim. The optional
`X-Earshot-Project-Id` and `x-platform-project-id` headers only assert the same
project; neither grants access. Earshot project rows must exist before data
requests. Provision service-side rows with the operator CLI
`earshot project create <project_id> --display-name ...`; there is no public
project-creation API. Platform's canonical project UUID can be the Earshot
project ID directly; no `platform_...` rewrite is used.

The receiver currently enforces these least-privilege scopes. The exact literal
names are Earshot's contract proposal and still require agreement from the
Platform token issuer before hosted acceptance.

| Scope                     | Earshot operations                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------- |
| `earshot:read`            | Incident list/detail, analysis, explanation, export, references, live list and SSE |
| `earshot:write`           | Incident/capture/checkpoint ingest and reference writes                            |
| `earshot:delete`          | Reference removal                                                                  |
| `earshot:artifact:delete` | Immutable incident artifact deletion                                               |
| `earshot:summary:read`    | Generic and optional Platform summary                                              |
| `earshot:project:delete`  | Project erasure                                                                    |

## Generic and Platform summaries

`GET /v1/projects/{project_id}/summary` is the host-neutral API. It returns up
to 50 `local_api`-exportable session metadata rows with `session_id`, `status`,
`framework`, `framework_truncated`, and `created_at_unix_nano`. Framework names
are capped at 128 characters; the boolean marks legacy values that were shortened.
It has no host labels, links, artifact bytes, transcripts, analyses, or references.

Platform response shaping is isolated in the optional
`earshot.integrations.platform` adapter. Enable it with
`EARSHOT_PLATFORM_ADAPTER_ENABLED=true` (or `earshot serve --platform-adapter`)
alongside `hosted_jwt` mode. It exposes
`GET /v1/platform/projects/{project_id}/observe/summary`, validates a canonical
lowercase UUID matching the signed project claim, and maps the generic rows to
`{summary, items}` with same-origin `/observe?sessionId=...` links. It does not
accept a global service bearer or create authority from a header. The adapter
is optional; generic Earshot installs do not depend on Platform.

The summary scan is bounded to 500 eligible catalog rows and returns up to 50
unique sessions, so duplicate-heavy history may yield fewer results. Listing
can trigger cleanup of that project's expired incidents; a large expired
backlog may make that request slower.

## Detail, live view, and Platform BFF

The browser capture transport is a cookie-authenticated client of the Platform
BFF. It sends required `x-earshot-project-id` and `x-earshot-auth-context-id`
assertions with each capture and retry. The BFF must derive project scope and
the current opaque auth-context version from the Platform session and reject a
mismatch before forwarding; neither header grants authority. The browser getter
must change its nonsecret opaque value whenever the effective user/project grant
changes. The transport is bound to its initial value and will not send retained
work under a different context. Platform's BFF keeps service tokens server-side.

The accepted v2 response supplies the host-owned `call_id`. The host's durable
mapping and seal outbox retain it with the canonical `projectId` and
`authContextId`. The BFF commits that mapping/outbox before returning success to
the browser; a browser callback is advisory and cannot be the durable owner. The
browser does not derive the Earshot call id from its raw session id. Seal
finalized calls through the BFF so each service keeps ownership of its own data
and retries.

If the seal response is ambiguous, the BFF checks both
`GET /v1/incidents?session_id=<call_id>` and `GET /v1/live/sessions` in that
project. A matching `finality: "final"` artifact alone does not prove cleanup:
Earshot can ingest the artifact and then fail to persist the durable capture
replay fence, returning `503 EARSHOT_CAPTURE_JOURNAL_UNAVAILABLE` while the
finalized live session and its capacity reservation remain. If the call is still
in the live list, retry the same idempotent seal even when the final artifact
already exists. Complete the outbox only after the matching final artifact is
present and the call is absent from the live list. A 404 by itself does not
prove failure because a successful seal drops the live buffer. If neither the
artifact nor live call is visible, or either reconciliation read is unavailable,
keep the outbox item pending for repair.

The reusable Observe viewer reads ordinary project-scoped endpoints:

| Purpose              | Earshot route                                               |
| -------------------- | ----------------------------------------------------------- |
| Session chooser      | `GET /v1/incidents?session_id=...`                          |
| Immutable artifact   | `GET /v1/incidents/{bundle_id}`                             |
| Analysis/explanation | `GET /v1/incidents/{bundle_id}/analysis` and `/explanation` |
| Live-session list    | `GET /v1/live/sessions`                                     |
| Live stream          | `GET /v1/live/sessions/{session_id}/tail` (SSE)             |

All hosted reads use `earshot:read`. Platform's authenticated Next.js BFF must
derive project scope from the Platform session, mint the short-lived
Earshot-audience JWT, and proxy the viewer APIs. It must forward
`Last-Event-ID`, stream SSE without buffering, and cancel the upstream stream
when the browser disconnects. Browser JavaScript receives only the Platform
session; service JWTs, signing keys, project API keys, and provider credentials
stay server-side.

Mount `ObserveFeature` on both the session route (`/observe?sessionId=...`) and
immutable artifact route (`/sessions/{bundleId}`). Provide
`ViewerQueryScopeProvider` with the canonical `projectId` and a nonsecret
`authContextId` that changes whenever the effective user/project grant changes.
All Earshot query keys and live stores include both values; old scoped queries
are cancelled and removed on scope change. `clearViewerQueries(queryClient)`
clears Earshot-owned cache entries on logout and preserves host-owned queries.
Platform owns the BFF and shared UI shell; they were not changed in this
Earshot checkout.

## Project deletion

`DELETE /v1/projects/{project_id}` requires `earshot:project:delete` and an exact
match with the signed project claim. Earshot persists `deleting` before
removing data, fencing subsequent reads, writes, and retries. The built-in
`default` project cannot be deleted. Cleanup removes Earshot incidents,
references, analyses/projections, connector retry receipts and configuration,
API keys, capture-call journals, and queued content objects attributable to those
incidents. Unknown CAS orphans stay preserved for explicit maintenance. Hashed
bundle-ID tombstones remain to prevent ID reuse.

`200` with `state: deleted` confirms the Earshot-owned cleanup. `202` with
`state: deleting`, `Retry-After: 3`, and `retry_after_seconds: 3` means writes
are fenced while storage cleanup is pending. Each request removes at most 500
selected incident rows, then at most 500 rows from each auxiliary table in order,
plus up to 500 queued CAS objects. Incident deletion can also cascade graph,
analysis, and reference rows. Cleanup returns pending between batches. SQLite
page compaction occurs after releasing the
application mutation lock; a busy WAL checkpoint also remains pending. Repeat the
same DELETE until it returns 200. Platform must keep overall deletion incomplete while any service
reports pending or failure. Earshot does not erase producer-owned checkpoint
source files, other services' records, backups, or snapshots; their owners must
acknowledge their own deletion.

Earshot retention currently defines expiry, not a minimum-storage hold. An
explicit project-erasure request overrides future expiry deadlines, consistent
with per-incident purge. There is no legal-hold mode in this contract; a product
that requires one needs an explicit hold state and pending response before it
can promise that behavior.

## Missing, delayed, and partial capture

Earshot only knows about calls whose producer registers a live journal or
submits an incident. A missing or delayed delivery has no incident to query, so
Platform remains the source of call delivery status. The viewer accepts a
host-reported `call-status` route for waiting, delayed, missing, or rejected
evidence and says Earshot has not verified an incident. Once an artifact is
available, its finality, completeness, recovery, coverage, and omission records
describe partial or withheld evidence. The UI does not invent a transcript or
represent absence as successful empty capture.

## Privacy and retention

The proposed runtime mapping is metadata-only by default. Transcript text,
audio, tool inputs/results, model prompts/responses, provider secrets,
diagnostic payloads, raw OTLP, and unconstrained attribute maps are excluded.
Expanding capture requires explicit policy, purpose, and retention terms.
Earshot does not ingest or serve audio bytes. Artifact retention governs
evidence lifetime; purge removes the immutable artifact, analyses, projections,
and external references. The viewer surfaces the privacy manifest and recorded
omissions alongside evidence.

## Viewer package and standalone use

The React feature is in the Earshot-owned, private `@earshot/viewer-ui` workspace
package, currently versioned `0.1.0`. React and React Query are peer dependencies;
the package has no React Router or Next.js runtime dependency. Next.js hosts transpile
the package and provide navigation. Platform's local integration links the package
source from the Earshot Task 3 checkout while it remains unpublished; publication is a
separate release step. Embedded hosts import `styles/embedded.css`, not the standalone
global styles.

The Vite `apps/viewer` remains bundled into the Python wheel and single-process
Docker image. `apps/viewer-next` remains an optional standalone Next.js host
using operator browser-session auth; it is not the hosted Platform BFF. API
types come from `spec/backend-api.openapi.json`.

## Handoff status

Earshot now has a distinct hosted JWT verifier, generic metadata summary, an
optional Platform adapter, project/auth-scoped viewer caches, authenticated
detail/live contracts, and project deletion. Scope literals and the runtime
event schema still need owner acceptance. Platform must provide per-request
JWT issuance, BFF streaming, viewer mounts, and account-level deletion
coordination. TVIC must approve/emit the versioned metadata envelope through the
Platform host. No cross-service acceptance is claimed until those owners run
the conformance flow against a real staged call.
