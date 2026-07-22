# Earshot pre-release first-principles audit

> Historical audit snapshot. The defects below describe the pre-hardening worktree.
> See [`pre-release-hardening-outcome.md`](pre-release-hardening-outcome.md) for the
> implementation outcome and current release stance.

Date: 2026-07-22  
Audited revision: `eb5a922048f6fe0b8a8573feb8b69d167c530415`  
Scope: Python SDK and adapters, canonical contracts, privacy, deterministic analysis,
ingestion API, connector trust boundaries, storage and migrations, viewer semantics,
packaging, container defaults, CI, and mature observability SDK source comparisons.

The accompanying source study is
[`external-observability-sdk-patterns.md`](external-observability-sdk-patterns.md). It
contains 92 commit-pinned source and test citations across LangSmith, Langfuse,
OpenTelemetry Python, and Sentry Python. Additional local inspection covered Phoenix,
OpenInference, the OpenTelemetry GenAI semantic conventions, and the Langfuse server.

## Executive verdict

**Earshot is a strong evidence engine inside a not-yet-production SDK. Do not freeze or
release the current public surface as v1.**

The codebase's unusual strength is real: it models what generic tracing systems usually
blur—clock domains, generated/sent/received/rendered boundaries, evidence provenance,
coverage and missingness, typed causal links, privacy governance, immutable artifacts,
and deterministic recomputation. The validation, content-addressed storage, connector
trust boundary, and analysis discipline are substantially better than a typical early
observability project.

The release risk is concentrated at the edges users will depend on forever:

- the default Compose deployment is vulnerable to DNS rebinding and anonymous API use;
- the Python SDK can silently lose incidents during reconfiguration, fork, process exit,
  saturation, and transport failure;
- secrets and malformed endpoint URLs are not handled as production SDKs handle them;
- the supposedly v1 contract and 82-symbol top-level API freeze too much while the
  distribution is still `0.1.0` alpha;
- latency validation and browser timeline construction can present false measurements;
- the frontend reconstructs explanations the backend should author;
- the package combines a lightweight in-process SDK with FastAPI, Uvicorn, storage,
  CLI, and the web server;
- the product has a final-artifact path but no live, standard OTLP path, which limits its
  usefulness as an observability platform.

The right response is not a rewrite. Preserve the evidence kernel and durable artifact.
Replace the public SDK/lifecycle seam, repair the release-blocking correctness and
security defects, add an explanation API, and keep a standard OTel path alongside the
final Earshot incident.

### Readiness by area

| Area                                  | Stance                       | Why                                                                                           |
| ------------------------------------- | ---------------------------- | --------------------------------------------------------------------------------------------- |
| Canonical evidence model              | Strong alpha                 | Differentiated provenance, missingness, clocks, causality, and privacy                        |
| Validation and deterministic analysis | Strong but not frozen        | Broad invariants, but a negative latency can still become measured truth                      |
| Local durable storage                 | Good alpha                   | CAS, digest checks, fsync, tombstones, rebuildable projections; backup-key contract is unsafe |
| Provider connectors                   | Promising                    | Strong raw-body authentication and receipts, but real captured delivery coverage is missing   |
| Framework adapters                    | Technically deep             | Preserve native OTel identity; lifecycle and compatibility surface are still manual/narrow    |
| Python SDK product surface            | Not releasable               | Global-only configuration, silent loss, fork/exit failures, no status/sampling/public flush   |
| Ingest transport                      | Alpha                        | Incident-level idempotency is good; no batch/compression/retry contract or live OTLP path     |
| Viewer                                | Prototype                    | Useful direction, but it invents stage windows and cannot securely authenticate remotely      |
| Deployment defaults                   | Not releasable               | DNS-rebinding/auth bypass and `.env` build-context exposure                                   |
| Test suite                            | Strong unit/integration base | High coverage, but missing lifecycle, real-provider, compatibility, and clean-release gates   |

## What the best SDKs actually do

The mature systems do not win because they provide a proprietary endpoint. They combine:

1. a two-line adoption path and a first-class explicit client;
2. a boring reliability core with bounded memory, context propagation, batching,
   diagnostics, sampling, fork handling, flush, and shutdown;
3. a stable semantic and compatibility contract;
4. a unique workflow built on the telemetry.

LangSmith's product workflow is traces connected to datasets, experiments, and
evaluations. Langfuse combines OTel interoperability with prompts, scores, media, cost,
and datasets. Sentry turns errors, performance, and telemetry loss into an operational
issue workflow. OpenTelemetry supplies the neutral seam.

Those projects are already moving into voice. The inspected LangSmith SDK has LiveKit,
Pipecat, OpenAI Realtime, and Google live-agent integrations. Its Realtime code derives
turns, transcripts, latency, interruptions, usage, and stereo audio. Therefore
“observability for voice” or “LangSmith for voice” is not a defensible first-of-kind
claim.

Earshot's defensible claim is narrower:

> An open, provider-neutral evidence contract in which a voice diagnosis is auditable:
> canonical boundaries, clock and correlation provenance, immutable evidence, explicit
> uncertainty and missingness, deterministic analysis, and an explanation showing why a
> conclusion is valid.

Transport ergonomics are the admission price. **Epistemic correctness**—refusing to
manufacture a precise answer from insufficient evidence—is the moat.

## Release-blocking proven defects

### P0-1: Default Compose trust can be bypassed through the Host header

`compose.yaml` sets `EARSHOT_HOST=0.0.0.0` and
`EARSHOT_TRUST_LOCAL_NETWORK=true`. Authentication is then disabled, and the loopback
Host-header check in `api.py` runs only when the configured host itself is loopback. A
request with `Host: attacker.example` to the Compose-equivalent configuration receives
`200` from `/v1/incidents` without credentials.

The loopback port publication does not close this boundary. DNS rebinding is precisely a
way for browser code to reach a loopback service while retaining an attacker-controlled
Host/origin name.

Required change:

- never let `trust_local_network` disable Host validation;
- enforce an explicit allowed-host list (`localhost`, loopback literals, and deliberately
  configured local names) based on the public request Host, independent of the bind host;
- preferably generate a local credential/session even in the default deployment;
- add a real default-Compose browser/rebinding regression test.

### P0-2: Docker sends the ignored local `.env` into the build

The repository has an ignored `.env`, but `.dockerignore` does not exclude `.env` or
`.env.*`. The web stage executes `COPY . .`, so credentials are sent to the Docker daemon
and become available to the stage, cache, remote builder, and package lifecycle scripts.
The final runtime image does not deliberately copy the file, but that does not make the
build boundary safe.

Required change: exclude `.env`, `.env.*`, `local/`, editor credentials, key material,
and other secret patterns; replace `COPY . .` with the smallest workspace manifests and
source directories the viewer build needs; add a build-context secret-canary test.

### P0-3: Reconfiguration silently strands active sessions

`earshot.configure()` replaces the global exporter and immediately shuts the previous
one down. Recorders created before reconfiguration keep the old exporter. The reproduced
sequence was:

```text
old worker before reconfigure: alive
old worker after reconfigure: stopped
old session close: export_accepted=False
old session last_export_error: None
```

This is silent incident loss caused by a supported public call. The documentation says
“configure once,” but the function does not enforce that contract.

Required change: make an explicit `Client` own resources and sessions. The convenient
global `init()` should be idempotent for equivalent configuration, reject conflicting
reinitialization while resources/sessions are active, and never close a resource still
owned by another recorder.

### P0-4: Forked processes accept exports that can never run

`BoundedAsyncExporter` inherits a dead thread and queue across `fork()`. In a real fork
check, the child reported:

```text
child worker alive: False
child submit accepted: True
child flush succeeded: False
```

Gunicorn preload, uWSGI, multiprocessing, task workers, and some model-serving setups can
hit this. OpenTelemetry, Langfuse, and Sentry all contain explicit fork machinery and
real fork regressions because thread/lock/connection inheritance is not theoretical.

Required change: register an after-fork hook or detect PID change; recreate locks,
queues, workers, process resource metadata, and SDK-owned HTTP pools in the child; clear
the inherited pending queue so children do not replay parent data; preserve caller-owned
transports.

### P0-5: Exit, flush, and telemetry-loss semantics are incomplete

The high-level SDK exposes `shutdown()` but not `flush()`, registers no atexit cleanup,
has no serverless synchronous/awaitable export mode, and hides exporter diagnostics from
the one-line `configure()` path. A daemon worker can disappear at interpreter exit.
`export_accepted=True` means only “entered an in-memory queue,” not “delivered.” A failed
send after that point is visible only if the user constructed the lower-level exporter
with a diagnostic callback.

The count-bounded queue also is not byte-bounded: 128 large final incidents can consume
very large memory. Recorder/adaptor per-session accumulators are generally unbounded
before serialization.

Required change:

- define `capture`, `flush(timeout)`, `shutdown(timeout)`, repeated shutdown, post-close
  capture, cancellation, atexit, fork, and serverless semantics as public contracts;
- expose `client.status()` with accepted/exported/retried/rejected/overflow/privacy/
  sampled/abandoned counters, queue bytes/depth/high-water mark, oldest age, last success,
  and non-sensitive last failure;
- cap both count and bytes for queues and all voice accumulators;
- represent an incomplete causal conversation as incomplete, not apparently complete.

### P0-6: Endpoint parsing and secret ownership are unsafe

`HttpExportTransport` validates only scheme/hostname and then appends
`/v1/incidents` to the original string. Userinfo, query, and fragment are accepted. The
following invalid normalized values were reproduced:

```text
https://user:secret@example.com?tenant=acme/v1/incidents
https://example.com/base?tenant=acme#frag/v1/incidents
```

`SdkConfig` is a frozen dataclass containing the plaintext token and is returned from
`configure()`. Its normal repr prints the token. `HttpExportTransport.token` is also a
public printable attribute.

Required change: parse and rebuild the URL from validated components; reject userinfo,
query, fragment, control characters, malformed ports, and ambiguous paths; accept either
a base URL or a fully specified endpoint through explicit configuration; store credentials
in a redacted secret wrapper or private field and ensure repr, diagnostics, exceptions,
and logs cannot disclose them.

### P0-7: A negative latency becomes valid measured truth

The generic `TurnRecorder.record_measurement()` accepts any finite number. A valid
incident containing `earshot.turn.response_latency = -250 ms` passes contract validation,
and deterministic analysis publishes it as:

```text
availability=available, confidence=measured, value=-250.0 ms
```

Generic quality values may legitimately be negative, so the fix is not a blanket
non-negative scalar. The semantic registry/validator must declare constraints by
measurement name and unit: latency/duration/counters non-negative, probabilities in
`[0,1]`, finite domains, and exact aggregation rules. The analyzer must defensively
refuse invalid semantic values even if an older artifact reached it.

### P0-8: The viewer manufactures stage duration

`apps/viewer/src/features/inspector/timeline.ts` explicitly turns point operations into
windows ending at the next stage's start; the last stage ends at its TTFB/TTFT value.
Those intervals are not evidence. They can label scheduling gaps or unrelated work as STT,
LLM, or TTS duration—the exact epistemic mistake the backend carefully avoids.

The same file converts decimal-string nanoseconds with `Number(value)` before
subtraction. For large monotonic values this loses integer precision; subtract with
`BigInt` first, then convert the bounded delta to milliseconds.

Required change: move stage/timeline construction into a versioned backend explanation
projection. Return true intervals only when both valid boundaries exist. Render point
facts as points; render unavailable duration as unavailable; distinguish direct,
derived, estimated, contradicted, and truncated/lost visually.

### P0-9: Media locator handling can bypass or crash credential checks

`locator_has_credentials()` is called on untrusted model values without defensive URL
parsing. `https://[` raises `ValueError`. Credentials placed in fragments or path
parameters are not recognized. `MediaLocator.uri` itself is just a non-empty string.

Required change: define one total, non-throwing locator parser with a strict portable
scheme/host/length/control-character policy; fail closed on any parse error; prefer opaque
content-addressed object references over provider URLs; fuzz the URL/secret corpus.

### P0-10: Missing backup correlation key silently changes identity

The ADR says the SQLite catalog, CAS, and `instance-correlation.key` are one backup unit
and correlation cannot be reconstructed without the key. The implementation creates a
new key whenever the file is absent, even when an existing valid database/CAS exists.
That silently breaks matching against stored external-identity fingerprints after a
partial restore.

Required change: create a key only for a provably new store. If catalog/CAS state exists
and the key is absent, fail closed with a restore instruction. Test full restore, database
without CAS, CAS without database, and database/CAS without key.

## Public-contract mistakes to correct before v1

### 1. The top-level API is too large and the wrong layer is public

`earshot.__all__` exports 82 names, including nearly every wire Pydantic model, codecs,
validation internals, recorder internals, analysis models, pipeline helpers, and global
SDK functions. Every exported name, model constructor, default, enum, nesting decision,
and exception becomes migration burden.

Freeze a small kernel instead:

```python
client = earshot.Client(...)       # explicit, testable, multi-project path
earshot.init(...)                  # env-configured convenience path, same implementation

with client.conversation(id=...) as conversation:
    ...

client.flush(timeout=...)
client.status()
client.shutdown(timeout=...)
```

Keep a low-level canonical event method and provider `instrument_*`/`wrap_*` helpers.
Move wire models to `earshot.types` or `earshot.contract`; advanced recorder and adapter
surfaces to explicit modules; unstable APIs to `earshot.experimental`.

The current `IncidentRecorder` has a large public recording surface and is a useful deep
internal module, not the ideal primary onboarding abstraction.

### 2. The global process configuration is not a sufficient client model

There is no first-class explicit SDK client, no env-based standard configuration, no
multi-project selection, no async client, no context propagation/suppression facility,
and no scoped ownership. Mature SDKs separate process resources, conversation/request
isolation, and current operation using token-restored context variables.

Add three internal layers:

1. process resource/configuration ownership;
2. conversation isolation (project, privacy, sampling, session identity);
3. current causal operation/turn/provider event.

Adapters and decorators should resolve the client once at the conversation boundary and
then carry explicit internal state. Test interleaved async conversations, threads, nested
contexts, cancellation, and multiple projects. Add a suppression context so internal
Earshot HTTP/storage activity cannot instrument itself recursively.

### 3. Manual operations fabricate disconnected OTel roots

Every `IncidentRecorder.operation()` creates a new random trace ID and span ID, without
parent context. Two operations in one manual session therefore look like two unrelated
OTel traces even though they are in one Earshot conversation.

Do not invent distributed tracing identity. Reuse an active W3C/OTel context when one
exists. Otherwise use native Earshot operation IDs and causal links; if a synthetic local
trace is deliberately created, make one session/root decision with explicit provenance.

### 4. The alpha package should not claim an exact stable v1 contract

The distribution is `0.1.0`, while the schema, semantic profile, API, pipeline adapter,
connectors, protobuf package, generated schema URLs, and docs repeatedly claim `v1` or
`1.0.0`. Validation accepts only the exact current schema/profile version. This is
prematurely expensive while known semantic and lifecycle errors remain.

Freeze these layers independently only after compatibility fixtures exist:

- public SDK API;
- canonical evidence/event schema;
- transport envelope and acknowledgement protocol;
- derived analysis version;
- adapter mapping identity and supported provider SDK range;
- storage migration version.

Until then, call the artifact/schema pre-v1 (`0.x`, `v1alpha`, or an explicit experimental
profile). `extra=allow` can help forward field compatibility, but exact-version rejection
still needs a major/minor compatibility policy and migration tooling.

### 5. The distribution bundles unrelated responsibilities

Base `earshot-observability` installs FastAPI and Uvicorn for every in-process SDK user.
The same package contains SDK capture, contract models, adapters, analysis, SQLite/CAS,
connectors, API, CLI, and web serving. This increases dependency conflicts, cold import,
security surface, and release coupling.

At minimum split extras:

```text
earshot-observability            lightweight API/core SDK
earshot-observability[otel]      OTel bridge
earshot-observability[livekit]   LiveKit adapter
earshot-observability[pipecat]   Pipecat adapter
earshot-observability[server]    FastAPI/Uvicorn/storage/CLI
```

A later separate server distribution may be cleaner, but do not create abstract storage
drivers until a second real backend is needed. `IncidentStore` can remain a concrete,
internal deep module.

The unrelated `earshot` PyPI distribution already owns `pip install earshot` and the
same import package. Keep the distribution name unambiguous in every command, test
co-install behavior, and decide before broad adoption whether the import namespace or
product packaging needs a change.

### 6. Clean release artifacts are not tested as users receive them

A wheel built from the dirty development tree after the viewer bundle step contains the
SPA. A wheel built from a clean `git archive HEAD`, exactly as the Python CI job can see
it, contains **zero** `earshot/web` assets. Hatch's optional artifact pattern silently
accepts the missing directory. The Python and TypeScript CI jobs are separate, so the
wheel job does not build the viewer.

Build one release artifact graph: compile the viewer, build wheel/sdist/image, install
each into a clean environment, run public-import/API/UI smoke tests, inspect contents,
and only then publish. Do not rely on a prior developer build directory.

## Transport and observability architecture

### Keep the final artifact, but add a live standard path

The ADR is correct that generic OTLP does not define voice-session completion, late-span
revision, multi-trace correlation, or a final portable incident. That is a reason not to
replace the incident artifact with OTLP. It is not a reason to offer no live observability
path.

Use two complementary paths:

```text
existing runtime OTel
  -> standard OTLP spans/events now
  -> user's existing collector/backend or Earshot live receiver

Earshot evidence kernel
  -> final canonical incident after explicit completion
  -> immutable storage, deterministic analysis, replay/regression artifact
```

This preserves user choice and gives long-running sessions immediate visibility. The
Earshot SDK should enrich the application's existing graph and never require a second
trace root. An eventual `/v1/traces` receiver must first specify project routing,
deduplication, completion/idle timeout, late data, bounded staging, crash recovery, and
privacy—as ADR 0003 already recognizes.

### Improve the incident exporter without copying generic trace semantics blindly

The current exporter sends one complete incident per HTTP request, serially, with fixed
backoff and no jitter, `Retry-After`, compression, batching, or durable spool. HTTP 408 is
classified as permanent. A slow request creates head-of-line blocking.

Add gzip/zstd where appropriate, byte-aware queues, jittered and interruptible backoff,
`Retry-After`, explicit retryable statuses, total deadlines, and optional disk spooling
for durable/serverless modes. Preserve the same bundle bytes and ID across retries. The
backend's bundle/digest conflict behavior already provides a good incident-level
idempotency base.

Do not copy an OTel “drop oldest span” policy for causal incidents. Losing the beginning
of a conversation while retaining later derived facts can create false conclusions.
Reject/drop the whole artifact or mark it incomplete and prohibit authoritative analysis.

### Make raw ingest replayable when live ingestion arrives

Langfuse persists raw OTLP resource spans in blob storage before asynchronous projection;
Phoenix accepts standard compressed OTLP and performs idempotent, out-of-order span
insertion. Earshot's final canonical artifact is already a stronger immutable authority
for completed sessions. For a future live path, persist bounded immutable raw/canonical
boundary events before projections so mapping bugs can be repaired and analyses replayed.

## Privacy and semantic-convention work

The default metadata-only posture, allowlisting, omission ledger, recursive portable-value
snapshotting, export governance, and refusal to claim a human “heard” output are major
strengths. Preserve them.

Before freezing the profile:

- separate audio, transcript, model/tool payload, raw provider payload, media locator,
  and identity controls in the easy SDK configuration;
- make custom masking recursive and fail closed before persistence or queueing;
- bound and flag raw strings, base64/audio, transcripts, unknown event caches, and media;
- make deletion cover queues/spools, blobs, derived analyses, and indexes;
- audit all 68 Earshot attributes against the current standalone OTel GenAI conventions
  and OpenInference, reusing `gen_ai.output.type=speech`,
  `gen_ai.response.time_to_first_chunk`, and `gen_ai.usage.*` where meanings match;
- remove or explicitly justify duplicates such as a custom total-token metric;
- version the exact external semantic-convention revision each adapter maps.

Earshot-specific names should describe only genuinely voice-specific facts: capture,
speech boundaries, turn commitment, generated/sent/received/rendered audio, playout,
interruption phases, transport evidence, clock alignment, coverage, and evidence quality.

## Backend and viewer changes

### Keep SQLite/CAS for the current scale

Do not replace SQLite with ClickHouse/Postgres merely because mature vendors use them.
The CAS authority, transactional graph/read-model publication, digest verification,
secure purge, rebuildable Turn Facts, and versioned analysis cache form a coherent local
product. Benchmark Earshot-shaped load first, then add a second backend behind a real
seam if required.

Before users store irreplaceable data:

- use explicit, durable migration fixtures for every released schema version, not only
  selected old versions;
- publish backup/restore and key-rotation procedures;
- test downgrade refusal and interrupted migration/reconciliation;
- centralize producer/adapter/analyzer/package versions instead of scattered literals;
- split the 3,005-line store and 2,207-line validator internally by responsibility where
  it improves testing, without exposing speculative driver abstractions.

### Do not block the ASGI event loop on scrypt

Project API-key authentication calls SQLite and `hashlib.scrypt(n=2^14)` directly inside
async middleware. Every authenticated request can block the event loop, making valid-key
traffic or deliberate invalid traffic a liveness issue. Move the work to a bounded worker
pool or redesign credential verification with a safe cache/revocation strategy. Add
concurrent auth/load tests.

### Make secure remote viewer authentication a product flow

The viewer's generated client always uses same-origin requests and has no bearer/session
credential flow. When remote access correctly requires a token or project key, the browser
cannot use it unless an external proxy injects credentials. Avoid long-lived API keys in
local storage. Add a server-created HttpOnly/SameSite browser session or an explicit local
operator login/exchange flow, CSRF protection, logout/expiry, and deployment docs.

### The backend must own explanations

The frontend should consume a response containing:

- canonical boundaries and true intervals;
- derived facts and evidence IDs;
- availability, basis, confidence, uncertainty, and limitations;
- contradictions, truncation, sampling, privacy omission, and SDK loss flags;
- analyzer/projection version;
- source clock domain and alignment status.

The UI then visualizes facts; it does not infer meaning from operation names, join samples
by substring, fabricate windows, or silently clamp impossible values.

## Adapter and ecosystem strategy

The LiveKit and Pipecat adapters contain thoughtful decisions: they reuse native trace
identity, preserve parentage and scope/resource provenance, distinguish metrics from
chronology, avoid duplicate operation ownership, and document framework ambiguities. This
is one of the codebase's strongest areas.

The adoption surface is still too manual. Provide one integration entry point per runtime
that attaches the correct processor/listeners, owns conversation close/abandon semantics,
and preserves application exceptions/cancellation/return values. Keep the current
low-level attachment APIs for advanced users.

Do not promise broad integration coverage yet. Publish a conformance suite and certify a
small number of adapters across minimum/current provider versions. The current CI supports
only Python 3.11 on Ubuntu and narrow Pipecat `1.5.x`/LiveKit `1.6.x` ranges; the package
claims `Python >=3.11`, which also implies untested future versions. Add an explicit
Python/OS/runtime/framework matrix and deprecation window.

Browser/mobile capture is strategically required, not optional polish. A server cannot
prove receive, decode, render start, playout completion, packet/jitter, or device clock
facts. A small browser/Node SDK should use generated contracts, exact integer time handling,
privacy-safe media/network observers, W3C propagation, and the same adapter conformance
model. The existing TypeScript schema/analysis packages are documented as superseded M0
prototypes; either remove them from the supported product story or replace them with an
authoritative client package.

## Verification performed

### Passing baseline checks

- Python: `790 passed`, `2 skipped`, `89.14%` branch-aware coverage.
- Ruff: passed.
- TypeScript: 40 tests passed (schema 4, analysis 8, viewer 28).
- TypeScript typecheck and production build: passed.
- Generated contract, OpenAPI, and semantic-registry checks: passed.
- `pnpm audit --prod`: no known vulnerabilities.
- Container hardening smoke at the audited revision: passed (non-root, read-only rootfs,
  API/UI ingest path).

### Failures and gaps observed

- Prettier check fails on two tracked viewer JSON fixtures.
- The real provider-delivery test is skipped because no captured provider deliveries are
  present.
- The local run skips the Groq headless import because `groq` is absent; CI installs that
  lane, but the release artifact matrix still needs clean installation tests.
- A clean source wheel has zero viewer assets.
- Default-Compose-equivalent arbitrary Host request returns anonymous `200`.
- Active-session reconfiguration causes silent export rejection.
- Forked child accepts an export with no live worker.
- Query/fragment/userinfo endpoint construction is malformed.
- `SdkConfig` repr prints the bearer token.
- Manual operations create unrelated trace IDs.
- Negative response latency validates and projects as measured.
- Malformed media URL parsing can raise.

High line coverage did not detect these because they are lifecycle and semantic-contract
tests, not ordinary branch tests.

## Required release gate

### Phase 0 — stop-the-line correctness and security

1. Fix Compose Host/auth trust and `.dockerignore` secret exposure.
2. Fix negative semantic measurements, media locator parsing, and missing-key restore.
3. Remove viewer-invented timing; serve backend-authored explanations.
4. Move scrypt off the ASGI event loop and design browser auth.
5. Add regression tests for every reproduced failure.

### Phase 1 — freeze the SDK and protocol deliberately

1. Introduce explicit `Client` plus convenient `init/get_client` over the same core.
2. Define lifecycle, context, fork, cancellation, atexit, serverless, and resource
   ownership semantics.
3. Add first-class status/loss reports, byte bounds, conversation-root sampling, and
   structural privacy controls.
4. Narrow the public kernel, split server dependencies, centralize versions, and move the
   canonical contract back to pre-v1 until compatibility is proven.
5. Specify transport acknowledgements/retries and clean build artifacts.

### Phase 2 — prove the evidence system end to end

1. Add a fake collector for duplicate/lost response, 408/429/5xx, Retry-After, slow and
   partial connections, invalid auth, queue pressure, and corrupt payloads.
2. Add real fork, interpreter exit, serverless one-shot, concurrent async/thread context,
   and memory-cap tests.
3. Add golden real-provider fixtures and minimum/current dependency lanes.
4. Add browser/client receive-render evidence and exact clock/BigInt handling.
5. Add historical schema/adapter fixtures and deterministic re-analysis/migration tests.

### Phase 3 — deliver the distinctive workflow

1. Add standard live OTLP interoperability alongside final incidents.
2. Make the evidence-first UI answer where time accrued and why that conclusion is valid.
3. Add versioned, recomputable voice analyses: turn decomposition, barge-in handling,
   silence, ASR finalization, tool detours, TTS first audio, playout, network/jitter,
   cross-talk, provider faults, and cost/quality tradeoffs.
4. Turn incidents into portable regression fixtures and comparisons across provider/model/
   release changes. That incident-to-regression loop is a stronger product wedge than a
   generic trace viewer.

## Final stance

The foundational idea is good enough to pursue. The current artifact model is more
careful about truth than the generic platforms, and that is the right place to be
different. But the repository is not “testing on the founder's end and then ready.” It is
an excellent alpha core with several externally exploitable or trust-breaking edge
conditions and an SDK contract that needs one deliberate redesign before adoption.

Fix the P0 list, freeze a small client/lifecycle contract, and make the frontend render
backend-authored evidence rather than inventing timing. If Earshot does that, it can claim
a meaningful category: auditable, portable causal evidence for voice systems. If it ships
mainly as automatic voice tracing plus a timeline, LangSmith, Langfuse, Phoenix, and OTel
are already close enough that the differentiation will not hold.
