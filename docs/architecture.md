# Architecture

## Product boundary

Earshot records **real-time voice sessions**, not only phone calls. A session may run
through a browser, mobile application, local device, WebRTC, raw WebSocket, SIP/PSTN,
or a framework such as Pipecat or LiveKit.

```text
voice runtime / application
  -> existing OTel + framework observer facts
  -> Earshot capture-policy filter
  -> Earshot profile enrichment (no second trace root)
  -> optional append-only checkpoint journal (one per open conversation)
       -> `earshot recover`: replay -> the same incident bundle, declared provisional
          when no close was observed
  -> immutable incident bundle
       -> local ingest: validation -> content-addressed storage -> SQLite index
       -> deterministic analysis: graph -> projections -> evidence-linked diagnosis
       -> governed local API/CLI and portable artifact export

hosted voice provider
  -> provider-authenticated finalized delivery
  -> connector kernel: raw-body trust -> receipt -> privacy-minimal normalization
  -> the same canonical incident and Turn Fact pipeline
```

The canonical graph is evidence. Turns and waterfalls are replaceable projections.
Analysis never mutates the immutable input artifact.

## Why Python for the M1 backend

The first two framework seams are Python. A single Python 3.11 implementation now
owns SDK capture, privacy filtering, contract validation, analysis, SQLite storage,
CLI, and the local API. This avoids two semantic authorities while the contract is
still alpha. Browser/viewer clients consume generated JSON Schema/protobuf bindings
and shared conformance fixtures.

The earlier TypeScript packages remain an M0 prototype and are not the contract authority.

## Artifact versus telemetry export

These are different operations:

1. A standard OTLP exporter sends traces/logs/metrics to an existing observability
   backend. This remains the application's parallel telemetry path.
2. Earshot packages a final, portable evidence snapshot for one voice session.

The incident endpoint is not a fake OTLP receiver. Streaming OTLP assembly requires
cross-request finalization and is outside M1. M1 adapters normalize ended spans and
callbacks into the profile; exact raw OTLP is retained only when a caller explicitly
supplies filtered bytes through the SDK.

## The two pluggable seams

Everything that produces evidence meets earshot at one boundary, and everything that
consumes an incident leaves through another. Both are named so an integration is a
registration rather than a change inside earshot.

**`ObservationSink` (`observation.py`) is where evidence enters.** A capture source --
the server pipeline, a provider stream adapter, a WebRTC/audio-graph engine, and in
future a browser, native, or backend collector -- authors governed facts through five
verbs and nothing else: `record_measurement`, `record_event`, `record_coverage`,
`record_omission`, `register_clock_domain`. Stage/operation authoring is deliberately
outside the protocol: minting an operation id and advancing a turn cursor is pipeline
bookkeeping that a fact-only collector has no way to model. `TurnRecorder` satisfies
the protocol structurally, so a capture source never depends on the pipeline session,
its turn ids, or its clock.

**A named exporter (`exporters/registry.py`) is where an incident leaves.** An
exporter takes a finished `IncidentBundle` and returns the document a backend
understands; built-ins register as `otlp` and `openinference`, and a user registers
their own beside them. The SDK client (`earshot.export(bundle, format=...)`) and the
CLI (`earshot export --format`) both select by name, and every named export is checked
against the exporter's declared export destination before it is projected. Registration
fills a dict and nothing else -- no network, no environment, no import-order effects.

## Trust boundaries

### Framework/runtime process

- May contain transcripts, prompts, tool payloads, identifiers, and credentials.
- Applies capture policy **before** values enter a queue, log, or serialized bundle.
- Preserves trace/span IDs and native parentage after filtering.
- Never performs synchronous network I/O in a voice-processing callback.

### Backend API and Connector boundary

- Treats every body as untrusted.
- Enforces size, media type, JSON duplicate-key, nesting, shape, invariant, and
  privacy-policy checks.
- Never dereferences a submitted media locator.
- Tokenless development binds to loopback and requires a loopback `Host` header.
- Remote access requires an explicit trusted-TLS-proxy deployment plus either a
  project API key or the legacy default-project bearer token. Container deployments
  bind inside the container network but publish the host port on loopback for the proxy.
- `/v1/*` operator/SDK routes use Earshot credentials. `/hooks/*` routes never accept
  those credentials as provider proof; each Connector authenticates the provider's
  documented raw delivery before JSON parsing.
- Tokenless loopback requests require a loopback `Host` header, closing the local
  DNS-rebinding boundary. Decode/canonicalization and fsync/SQLite ingest run in a
  worker thread so storage contention cannot stall ASGI liveness.

### Storage

- Exact canonical protobuf bytes live in a content-addressed object store and own
  evidence/graph content truth.
- SQLite is the durable publication/catalog/tombstone authority and contains
  project-scoped credentials, Connector/Delivery Receipts, privacy-safe indexes,
  rebuildable Turn Facts, and versioned derived analysis. CAS and SQLite are one backup
  unit together with `instance-correlation.key`; none is independently reconstructable.
- Operations, typed links, and events are transactionally projected into relational
  graph indexes; the canonical protobuf remains the source of truth.
- Evidence is immutable; legal/privacy deletion physically purges it and leaves a
  content-free tombstone.
- The earliest retention deadline of any captured class governs the immutable bundle
  and is enforced on startup and every read/list boundary.
- Object digests are rechecked on every read.

## Time and causality

There is no globally truthful distributed monotonic clock. Monotonic time is
authoritative only inside its declared clock domain. Cross-domain alignment requires
an explicit transform and uncertainty; without one, latency is unavailable.

OTel parentage represents true nested work. Typed links represent causal
relationships that are not a tree: retries, supersedes, produces/consumes,
interruptions, handoffs, and duplicates. Missing external parents or links are valid
in partial traces; missing targets declared `internal` are invalid.

## Capture-to-render semantics

The vocabulary covers:

```text
capture -> VAD -> turn detection -> STT? -> agent/LLM/tools? -> TTS?
        -> encode -> transport send/receive -> decode -> render
```

Native speech-to-speech systems may not expose STT/LLM/TTS stages. Coverage states
distinguish available, unsupported, not exposed, disabled, permission denied, and not
applicable. Absence is never fabricated as a zero-duration operation.

The strongest output facts are `generated`, `sent`, `received`, and `render_started`.
Earshot never defines `heard_at`: device render does not prove human perception.

The server pipeline is shared by in-app and telephony agents. Transport-specific
facts are additive: WebRTC/browser render evidence for in-app sessions and SIP/PSTN
gateway/carrier evidence for calls. Neither transport is required by the core model.

## Repository map

```text
proto/earshot/v1alpha1/incident.proto protobuf envelope
semconv/earshot.yaml                  authoring vocabulary
spec/incident-bundle.schema.json      generated debug-JSON schema
spec/derived-analysis.schema.json     closed analysis-sidecar schema
spec/backend-api.openapi.json         generated HTTP API contract
packages/sdk-python/src/earshot/
  contract.py                         Pydantic structural models
  validation.py                       cross-record invariant validator
  codec.py                            canonical JSON/protobuf codec
  privacy.py                          metadata allowlist and omission ledger
  observation.py                      ObservationSink capture seam
  recorder.py                         framework-neutral SDK recorder
  exporter.py                         bounded fail-open exporter
  exporters/registry.py               named, pluggable incident projections
  adapters/                           Pipecat and LiveKit mappings
  engines/                            deterministic browser-telemetry engines
  connectors/                         hosted-provider trust + normalization
  analysis.py                         deterministic projections/diagnoses
  storage.py                          SQLite + content-addressed store
  api.py / cli.py                     local backend surfaces
apps/ingest/app.py                    ASGI deployment entry point
Dockerfile / compose.yaml             single-image self-host path
fixtures/                             shared conformance and golden artifacts
```

## Failure model

Recorder and adapter failures are observability failures, not application failures.
Callbacks filter and enqueue without network I/O. Queue overflow returns a visible
negative submission result and a non-sensitive diagnostic. User diagnostic callbacks
run outside exporter locks, and recorder-finalization failures never replace an
application exception already in flight.

Storage has the opposite posture: it fails closed. An artifact is not reported as
ingested until its fsynced CAS object, incident row, graph projection, retention
deadline, and export projection are coherently published. Corruption, an incomplete
purge, or a busy WAL scrub is explicit and retryable.
