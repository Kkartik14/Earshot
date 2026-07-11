# Incident bundle contract v1

Status: **v1 alpha**. Wrapper versions use semantic versioning; the Earshot semantic
profile has an independently pinned version.

## One immutable snapshot

One bundle is a final evidence snapshot for exactly one voice session. `bundle_id`
identifies the snapshot; `session_id` identifies the voice session. They must not be
treated as interchangeable, and one future session may have multiple snapshots.

The custom protobuf envelope contains:

```text
IncidentEnvelope
  schema_version
  bundle_id
  session_id
  canonical_profile_json
  raw_otlp_chunks[]
  profile_sha256
```

`canonical_profile_json` uses the RFC 8785 JSON Canonicalization Scheme (JCS),
snake_case Earshot fields, decimal-string nanoseconds, and lowercase hex OTel IDs.
JCS fixes object ordering, string escaping, and IEEE-754 number spelling across
implementations; integers stored as JSON numbers are limited to the interoperable
53-bit domain. Exact 64-bit nanoseconds remain decimal strings. The envelope is
deterministically serialized. Each raw OTLP chunk preserves the exact caller-supplied
bytes only when the explicit `raw_otlp` capture class is enabled.

The canonical data-model projection uses snake_case field names, includes declared
defaults, omits every declared field whose value is null, and retains non-null profile
extensions. Declared optional null and absence are therefore equivalent. Null-valued
extensions are invalid because they could not survive this projection. The outer JSON
wrapper and raw-chunk records are closed; forward-compatible extension data belongs
inside profile records, where it survives JSON and protobuf round trips.
Protobuf decoders must re-render the parsed profile and require byte-for-byte equality
with `canonical_profile_json`; digest-valid whitespace, alternate number spelling, or
omitted-default encodings are noncanonical and rejected.

The JSON import/export representation contains the same profile plus base64-encoded
OTLP chunks. JSON is for fixtures, debugging, and interoperability; protobuf is the
canonical stored representation.

## Profile records

### Manifest and session

The manifest records independent `schema_version` and `semantic_profile_version`
pins, bundle/session identity, exact creation time, producer/adapters, and
completeness/finality. It does not invent one global OTel resource because a session
may contain several OTel resources.

The session has an open status vocabulary and start/end `TimePoint`s.

### Participants and audio streams

Participants use opaque IDs and open roles (`user`, `agent`, `human_operator`,
`system`, or future values). Endpoint kind is also open. Audio streams reference a
participant, direction, optional format, and optional transport identity. Participant
and stream attributes are governed by a record-level `capture_class`, defaulting to
`metadata`; sensitive retained attributes require the matching declared policy.

There are no caller/callee assumptions.

### Clock domains and time points

Every monotonic value references a declared clock domain. A time point may contain:

- source Unix nanoseconds;
- collector-observed Unix nanoseconds;
- monotonic nanoseconds inside a declared domain; and
- an uncertainty bound.

Nanoseconds are non-negative canonical decimal strings. This preserves values beyond
JavaScript's safe-integer range.

An interval is rejected only when comparable timestamps in the same clock domain
prove that its end precedes its start. Globally out-of-order records are valid.

### Coverage

Coverage makes missing evidence explicit:

```text
signal: render
availability: not_exposed
reason: server_cannot_observe_client_render
```

Non-available coverage requires a reason. Unknown availability values survive for
forward compatibility.

### Operations, events, and quality

- Operations are normalized indexes over OTel spans and preserve original trace,
  span, parent, links, resource attributes plus resource schema URL, and instrumentation
  scope name/version/attributes plus its independent schema URL when policy permits.
- Independently timestamped Earshot events map naturally to OTLP log records with
  source and observed timestamps; their resource/scope provenance is preserved too.
- Quality samples are numeric/boolean observations with explicit units and aggregation,
  preserve resource/scope provenance, and require evidence provenance. Arbitrary string
  values and raw counters are not quality data in v1.

Operation/event/framework/provider names use an open semantic-code vocabulary. Unknown
future values and non-null profile extensions are preserved only under the explicit
`extension_payload` policy. Canonical OpenTelemetry registry schema URLs are ordinary
metadata; third-party credential-free HTTPS schema URLs use that extension grant or
are represented only by a digest. `raw_otlp` grants only exact opaque chunks and is
not a valid normalized record class. Producer-controlled IDs and valid semantic codes
are a trust boundary and must not carry user content.

### Media

Media is always external. The bundle records a logical media ID, digest, content
type, size, governed stream association, optional byte/time range, and an optional
separately governed locator. Validation never fetches a locator.

Credential-bearing locators are invalid. A media reference is always governed by the
`audio` capture class. A destination restriction rejects the export rather than
silently producing a different artifact with a stale digest.

### Raw OTLP chunks

Each chunk declares signal, content type, compression, bytes and SHA-256. Its privacy
class is always `raw_otlp`; callers and imported artifacts cannot relabel opaque bytes
as metadata, and normalized records cannot borrow the class. A digest mismatch is
invalid. Raw/unfiltered OTLP requires an explicit
capture-policy grant because native framework telemetry may already contain content.
The JSON and protobuf wire forms require the digest. An in-memory SDK chunk may omit it
before encoding; the encoder computes it, while untrusted wire input never receives
that normalization privilege. Automatic serialized-OTLP interception is not an M1
capture path.

### Derived analysis binding

V1 analysis is a sidecar and `profile.analysis` must be absent. The sidecar's
`DerivedAnalysis.input_sha256` is the SHA-256 of the exact deterministic protobuf
artifact, including any governed raw OTLP chunks. The API and storage boundaries
recheck the analyzer version, generation time, and input digest before caching or
returning analysis. Sidecar records are closed and metadata-only; all projection
evidence refs, clock-domain keys, units, measurement names, source counts, and session
ownership are checked. The deprecated profile field is reserved only so decoders can
return a stable validation error instead of silently accepting embedded prose.

## Stable validation layers

1. **Structural:** Pydantic/JSON Schema/protobuf decoding.
2. **Semantic:** bundle IDs, references, graph, clocks, evidence, hashes, privacy,
   media, and analysis/input consistency.
3. **Ingest policy:** body limits, strict JSON, authentication, immutable identity,
   and storage integrity.

Validation issues have stable codes and paths and never need to echo source values.

## Required failures

- Unsupported schema version or malformed/all-zero OTel IDs.
- Duplicate owned IDs or duplicate `(trace_id, span_id)` facts.
- Session mismatch or dangling internal participant/stream/media/event references.
- Parent or causal cycle for relationships that must be acyclic.
- Same-clock reversed intervals.
- Render/transport/perceptual claims without provenance.
- Network jitter/loss/RTT attributed to audio inference.
- P.563 classified as network rather than perceptual MOS-LQO.
- Payload/media/OTLP present while its capture class is denied.
- Media references mislabeled as metadata and error messages mislabeled as metadata.
- An operation/event/quality record naming a participant who does not own its stream.
- Non-finite numbers, bad hashes, reversed byte ranges, or embedded `heard_at` claims.

## Required valid cases

- Arbitrary serialization order and parallel sibling spans.
- External/unresolved parents and cross-trace links declared external/unknown.
- Unknown operations, events, roles, endpoints, providers, and attributes.
- Native speech-to-speech sessions without fake STT/LLM/TTS spans.
- Explicit missing/not-observed coverage.
- A measured value of zero.

## Regeneration

```bash
python scripts/generate_contract.py
python scripts/generate_contract.py --check
```

This regenerates Python protobuf bindings, `spec/incident-bundle.schema.json`, and
`spec/derived-analysis.schema.json`. Conformance fixtures verify semantic invariants
that JSON Schema cannot express.
