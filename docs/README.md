# Earshot public documentation

These documents are the public, reproducible source of truth. They are sufficient
to implement another producer or consumer without access to `docs/private`.

## Start here

- [Architecture](./architecture.md) — boundaries, trust model, and repository layout.
- [Incident bundle contract](./incident-bundle.md) — canonical protobuf envelope and
  JSON representation.
- [Semantic profile](./semantic-conventions.md) — `earshot.*` operations, events,
  links, coverage, and evidence vocabulary.
- [Privacy](./privacy.md) — metadata-only default, filtering, omissions, retention,
  and safe export.
- [Backend API](./backend-api.md) — project-scoped ingest, retrieval, metrics, and purge.
- [Hosted-provider connectors](./connectors.md) — trust, replay, normalization, and
  provider-specific evidence limits.
- [Deterministic analysis](./analysis.md) — projection algorithm, evidence binding,
  and current diagnosis boundary.
- [SDK and recorder](./sdk.md) — one-line setup, recorder lifecycle, adapters, and
  fail-open guarantees.
- [Storage and retention](./storage.md) — CAS/SQLite transaction ordering, graph
  projections, expiry, reconciliation, and erasure limits.
- [Conformance](./conformance.md) — required fixtures and release gates.
- [Development](./development.md) — setup, regeneration, tests, and local operation.
- [Release packaging](./release.md) — distribution identity, build verification, and
  publication guardrails.
- [Pipecat adapter](./adapters/pipecat.md) and
  [LiveKit adapter](./adapters/livekit.md) — adapter contracts and limitations.
- [Raw provider adapters](./adapters/providers.md) — Deepgram, Cartesia, OpenAI
  Realtime, and Sarvam event mapping for custom in-app pipelines.

## Normative sources

The machine-readable contract consists of:

- [`proto/earshot/v1/incident.proto`](../proto/earshot/v1/incident.proto)
- [`semconv/earshot.yaml`](../semconv/earshot.yaml)
- [`spec/incident-bundle.schema.json`](../spec/incident-bundle.schema.json)
- [`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json)
- [`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json)
- shared valid/invalid fixtures under [`fixtures/`](../fixtures/)
- deterministic fault and security-regression artifacts under
  [`fixtures/faults/`](../fixtures/faults/)

If prose conflicts with a machine-readable source plus a conformance fixture, the
machine-readable contract wins. Bundle-wide semantic invariants are defined in the
contract document and enforced by `earshot.validation`.
