# Conformance and release gates

Passing JSON Schema is necessary but insufficient. A conforming implementation must
pass structural, semantic-invariant, privacy, codec, persistence, and analysis tests.

## Shared fixtures

```text
fixtures/
  valid/
  invalid/mutations.json
  golden/pipecat_spans.json
  golden/livekit_metrics.json
  conformance/canonical-vector.input.json
  conformance/canonical-vector.expected.json
  faults/*.incident.json
  faults/security_regressions.json
```

Each invalid fixture changes one invariant and records its expected stable issue code.
Each golden runtime fixture normalizes to the same explicit semantic projection after
allowlisted adapter-specific resource fields are removed.

The canonical vector exercises declared-null normalization, defaults, negative zero,
exponent formatting, Unicode, and nested profile data. Its expected file pins the
exact RFC 8785 profile text, profile digest, and deterministic protobuf-envelope
digest for independent implementations.

Every row in `faults/scenarios.json` has a deterministic, fully valid incident named
`<id>.incident.json`. The corpus covers endpointing, barge-in, individual pipeline
delays, retry causality, WebRTC transport evidence, reconnect, device absence, native
S2S without fake stages, privacy opt-out, and a SIP/PSTN bot-to-human handoff with
DTMF and voicemail gateway events. Assertion tests compare fast/slow commit delay,
order each barge-in boundary, localize the longest STT/LLM/TTS stage, prove retry
downstream causality, require direct loss/jitter/RTT provenance, retain duplicate and
out-of-order identity links, and make device capture/render absence explicit.

## Contract gates

- Trace IDs are 16 nonzero bytes/32 lowercase hex; span IDs are 8 nonzero bytes/16
  lowercase hex.
- IDs, parentage, links, extension-authorized unknown attributes, resource attributes
  and schema URL, scope name/version/attributes and schema URL, and exact uint64
  nanoseconds survive JSON/protobuf/storage round trips.
- Unknown future operation/event/provider/framework semantic codes survive. Typed
  labels and opaque IDs are producer-controlled metadata and must not contain user
  content; raw source labels are allowlisted or represented by exact SHA-256 labels.
- Child-before-parent, arbitrary array order, parallel siblings, and external links
  are valid.
- Duplicate identities, internal dangling references, cycles, and same-domain reversed
  intervals fail with stable codes.
- No supported API or analysis field claims `heard_at`.

## Privacy gates

Metadata-only fixtures inject sentinel secrets into governed payload fields—transcript,
audio locator, prompt,
completion, tool payload, diagnostic text, and unknown framework fields. Sentinels
must not appear in artifacts, raw OTLP, indexes, errors, diagnostics, or analysis.

This does not claim automatic PII detection in producer-owned opaque IDs, service/model
names, policy identifiers, or syntactically valid semantic codes. Conforming producers
must make those fields pseudonymous/non-PII; validators enforce shape and known payload
boundaries, not the semantic intent of an arbitrary identifier.

Media locators are never fetched. Credential-bearing locators fail validation.
Raw/unfiltered OTLP requires explicit policy permission and cannot be labeled as
metadata. That permission never grants unknown normalized attributes; those require
`extension_payload`. Participant and stream attributes must match their record capture
class.

## Analysis gates

- Missing stage is unavailable/not observed, never zero or failure.
- Native speech-to-speech without STT/LLM/TTS analyzes successfully.
- Fine point events override coarse span timing.
- Render starts well after TTS generation in the main regression fixture; response
  uses render and a fallback is explicitly `tts_estimate`/`transport_estimate`.
- Cross-domain latency without alignment is unavailable.
- Negative same-domain latency is inconsistent, never clamped.
- Parallel tools report total work separately from per-domain elapsed wall time.
- Every diagnosis cites evidence; missing evidence appears as a limitation.
- Every available metric carries source evidence; unavailable metrics cannot assert a
  value or unit. Every projected turn is unique and backed by source records.
- Every projection operation/event/quality reference exists in the exact input;
  unassigned provider measurements cite real quality samples and summaries match
  immutable source counts.
- Tool, interruption, provider-quality, and known failure-diagnosis evidence must be
  type-correct and owned by the projected turn it supports.
- Array permutation does not change semantic analysis.

## Persistence/API gates

- JSON and protobuf produce the same canonical artifact.
- Exact retry is idempotent; same ID/different content conflicts.
- Invalid input makes no database or object-store mutation.
- Concurrent identical/conflicting ingests have deterministic outcomes.
- Process restart preserves exact bytes and analysis/input digest association.
- Corruption is detected on read.
- Purge removes artifact and analysis and leaves only a tombstone.
- CAS cleanup cannot race an in-flight ingest; startup reconciliation repairs derived
  graph/export/retention projections but preserves unreferenced evidence for explicit
  operator review. Missing catalog + nonempty CAS fails closed.
- Expired incidents are purged on startup/read/list and cannot be returned in the
  interval before a maintenance job runs.
- Secure purge scans find no sentinel in SQLite/WAL or live CAS files, subject to the
  physical-media caveats in the privacy document.
- Duplicate JSON keys, invalid constants, excessive nesting, body overflow, malformed
  protobuf, bad media types, and unsupported encodings fail cleanly.

## SDK/exporter gates

- Voice callbacks never block on network I/O.
- Queue overflow and exporter failure create non-sensitive diagnostics.
- Diagnostic callbacks may re-enter the exporter without deadlock.
- Cross-origin redirects never receive bearer credentials; non-loopback HTTP export
  endpoints are rejected.
- Retry is bounded and idempotent.
- Exceptions/cancellation close a recorder exactly once, and recorder failure never
  masks the application exception.
- Concurrent sessions remain isolated.
- Optional framework packages are not imported by `import earshot`.

## Adapter-equivalence gate

One deterministic scenario must produce valid Pipecat and LiveKit artifacts with the
same shared facts for core response-stage classification, timing-basis vocabulary,
accepted/detected/ignored interruption phases, coverage, privacy, provenance, and
turn-correlated response work. Raw telemetry, opaque turn IDs, and provider durations
are not expected to be identical. Pipecat's complete `turn` lifecycle is not
`turn_detection`; where it exposes no equivalent commitment point, the latency value
must remain explicitly unavailable rather than being forced to match LiveKit EOU.

With framework extras installed, the integration lane must also construct current
LiveKit metrics objects and consume real OpenTelemetry `ReadableSpan` sibling topology
under the pinned Pipecat instrumentation scope.

## Adapter conformance and compatibility matrix

`packages/sdk-python/tests/adapter_conformance.py` is the reusable public-seam
conformance harness. It is applied to every shipped adapter family:

- Deepgram, Cartesia, OpenAI Realtime, and Sarvam use sanitized synthetic streaming
  payloads through `adapt -> apply -> close`.
- ElevenLabs, Vapi, Retell, and Ringg use the checked-in sanitized synthetic finalized
  Delivery builders through `HostedProviderIngestion`. These are not represented as
  captured provider deliveries; the opt-in real-delivery test retains its explicit skip
  when no operator-supplied payload is present.
- Pipecat and LiveKit consume their golden public surfaces, while installed-dependency
  lanes additionally construct real framework metric/span objects.

The harness validates canonical JSON/protobuf stability, deterministic normalized
output, native trace/span identity and parentage, sensitive-value absence, privacy
manifest behavior, complete and incomplete close, and semantic validation. Streaming
adapters record field-level omissions. Finalized connectors discard sensitive provider
fields before recorder admission, so their gate instead requires every sensitive
capture class to be `deny`/not-captured and scans both codecs for sentinels.

Recorder operation contexts are covered for exception and `CancelledError` identity:
closing cannot replace the application exception. Earshot does not ship a callable
decorator/wrapper, so callable return-value preservation is currently not applicable;
any future wrapper must add that assertion before release.

CI runs dependency-free adapter conformance on Linux with Python 3.11, 3.12, and 3.13.
It runs Pipecat and LiveKit in separate jobs against both their exact supported minimums
and the newest versions resolvable inside the declared ranges. Separate minimum jobs are
required because the frameworks impose different effective OpenTelemetry lower bounds.
One current-dependency Python 3.12 macOS smoke lane covers the combined install.
Dependency-version lanes are CI gates; a local environment normally proves only the
single set of versions it has installed.

The release gate is:

```text
goldens validate
  + protobuf/JSON round trip
  + persistent restart retrieval
  + deterministic evidence-linked analysis
  + metadata-only leak scan
  + Pipecat/LiveKit semantic equivalence
```

## Commands

```bash
pytest
pytest -m unit
pytest -m integration
pytest -m e2e
pytest --cov=earshot --cov-report=term-missing
ruff check .
python scripts/generate_fault_fixtures.py
python scripts/generate_contract.py --check
python scripts/generate_openapi.py --check
python scripts/check_semconv.py
pytest -q packages/sdk-python/tests/test_adapter_conformance.py
pytest -q apps/ingest/tests/test_finalized_adapter_conformance.py
pytest -q apps/ingest/tests/test_framework_integrations.py
```
