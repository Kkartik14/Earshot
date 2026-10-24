# Earshot public documentation

These documents are the public, reproducible source of truth. They are sufficient
to implement another producer or consumer without access to `docs/private`.

The current release is a pre-v1 alpha, 0.3.0 ([release notes](./release-notes-0.3.0.md)).
What Earshot receives today is listed in the repository
[README](../README.md#what-earshot-receives-today); mobile capture, OTLP over gRPC, and
regression-fixture export are planned and are not shipped APIs.

## Run Earshot

- [OTLP quick start](./otlp-quickstart.md) — the HTTP exporter, API keys and the host
  rule, session keys, close timing, recordings, Pipecat and LiveKit snippets, the Python
  `earshot.client` and `@earshot/node`, and errors and retries.
- [The call view](./calls.md) — audio and waveform, the placed transcript, reported
  latency and the audible gap at the agent, signals, cost, prices, and the calls list's
  filters and transcript search.
- [Storage and blobs](./storage-and-blobs.md) — SQLite or Postgres, migrations, upgrading
  from 0.1.x, the correlation key, and local or S3 blob URLs.
- [Production notes](./production.md) — one server per database, no transaction-pooling
  proxy, a Collector in front, S3 lifecycle, recording analysis and the `signals` extra,
  projections and prices, the transcript search index, readiness, and backups.
- [Server configuration](./configuration.md) — every variable of `earshot serve` and
  the ASGI app, before and after 0.2.0.
- [Changelog](../CHANGELOG.md) — what changed in each release, breaking changes first;
  the [0.3.0 release notes](./release-notes-0.3.0.md) add what its tests prove and don't.

## Start here

- [Architecture](./architecture.md) — boundaries, trust model, and repository layout.
- [Incident bundle contract](./incident-bundle.md) — canonical protobuf envelope and
  JSON representation.
- [Semantic profile](./semantic-conventions.md) — `earshot.*` operations, events,
  links, coverage, and evidence vocabulary.
- [Privacy](./privacy.md) — Capture Presets, the producer capture policy, omissions,
  media, call purge, retention, and erasure limits.
- [Backend API](./backend-api.md) — project-scoped ingest, retrieval, metrics, and purge.
- [Hosted-provider connectors](./connectors.md) — trust, replay, normalization, and
  provider-specific evidence limits.
- [Deterministic analysis](./analysis.md) — projection algorithm, evidence binding,
  and current diagnosis boundary.
- [SDK and recorder](./sdk.md) — one-line setup, recorder lifecycle, adapters, and
  fail-open guarantees.
- [Storage internals](./storage.md) — CAS/SQLite transaction ordering, graph
  projections, expiry, reconciliation, and erasure limits.
- [Conformance](./conformance.md) — required fixtures and release gates.
- [Retained real captures](./captured-fixtures.md) — captured-adapter provenance,
  privacy scrubbing, validation, and unavailable real-evidence gaps.
- [Development](./development.md) — setup, regeneration, tests, and local operation.
- [Release packaging](./release.md) — distribution identity, build verification, and
  publication guardrails; the owner's [release checklist](./release-checklist.md).
- [Architecture decisions](./adr/) — ADRs 0001 to 0009, including capture presets, the
  OTLP receiver, storage, the Call, recording analysis and call projections, and the cost
  model.
- [Pipecat adapter](./adapters/pipecat.md) and
  [LiveKit adapter](./adapters/livekit.md) — adapter contracts and limitations.
- [TVIC runtime adapter](./adapters/tvic.md) — runtime observation mapping,
  chronology, and loss boundaries.
- [Pipecat provider matrix](./pipecat-provider-matrix-2026-10-20.md) — current live
  Cartesian-product evidence and its limits.
- [Raw provider adapters](./adapters/providers.md) — Deepgram, Cartesia, OpenAI
  Realtime, and Sarvam event mapping for custom in-app pipelines.

## Normative sources

The machine-readable contract consists of:

- [`proto/earshot/v1alpha1/incident.proto`](../proto/earshot/v1alpha1/incident.proto)
- [`semconv/earshot.yaml`](../semconv/earshot.yaml)
- [`spec/incident-bundle.schema.json`](../spec/incident-bundle.schema.json)
- [`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json)
- [`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json)
- shared valid/invalid fixtures under [`fixtures/`](../fixtures/)
- deterministic fault and security-regression artifacts under
  [`fixtures/faults/`](../fixtures/faults/)
- privacy-scrubbed real-capture artifacts and their inventory under
  [`fixtures/captured/`](../fixtures/captured/)

If prose conflicts with a machine-readable source plus a conformance fixture, the
machine-readable contract wins. Bundle-wide semantic invariants are defined in the
contract document and enforced by `earshot.validation`.
