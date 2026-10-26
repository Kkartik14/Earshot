# Earshot documentation

These guides explain how to run Earshot, send it evidence, and understand what it stores.
Start with the [project README](../README.md) for the quick overview. The current release
is the pre-v1 alpha, 0.3.0. See the [release notes](./release-notes-0.3.0.md) for its
features and limits.

## Start here

- [OTLP quick start](./otlp-quickstart.md): send spans, upload recordings, and read calls.
- [The call view](./calls.md): explore audio, transcripts, turns, latency, and cost.
- [Privacy](./privacy.md): choose a capture preset and understand retention, purge, and
  erasure limits.
- [Production notes](./production.md): plan the server, database, proxy, backups, and
  retention.

## Connect an application

- [Python SDK and recorder](./sdk.md): record a session, use adapters, and export an
  Incident.
- [Browser SDK](../packages/browser/README.md): capture browser media and network
  measurements.
- [Node SDK](../packages/node/README.md): add a span processor and read recordings or
  calls.
- [Backend API](./backend-api.md): send evidence, read stored Incidents, follow live
  sessions, and manage projects.
- [Hosted-provider connectors](./connectors.md): understand signature checks,
  normalization, and provider evidence limits.
- [Pipecat adapter](./adapters/pipecat.md), [LiveKit adapter](./adapters/livekit.md),
  and [TVIC runtime adapter](./adapters/tvic.md): see what each integration records.
- [Raw provider adapters](./adapters/providers.md): map Deepgram, Cartesia, OpenAI
  Realtime, and Sarvam events into an Incident.
- [Pipecat provider matrix](./pipecat-provider-matrix-2026-10-20.md): review compatibility
  evidence and its limits.

## Understand the data

- [Architecture](./architecture.md): learn the system boundaries, trust model, and
  repository layout.
- [Incident bundle contract](./incident-bundle.md): read the portable protobuf envelope
  and JSON format.
- [Semantic conventions](./semantic-conventions.md): look up operations, events, links,
  coverage, and evidence terms.
- [Deterministic analysis](./analysis.md): see how Earshot builds projections and
  findings.
- [Storage internals](./storage.md): understand indexes, blobs, cleanup, and erasure.
- [Retained real captures](./captured-fixtures.md): review the published artifact
  inventory and the checks used to scrub it.
- [Conformance](./conformance.md): find the fixtures and release checks that protect
  compatibility.

## Develop and release

- [Development](./development.md): set up the repository, run the application, and update
  generated contracts.
- [Server configuration](./configuration.md): configure `earshot serve` and the ASGI app.
- [Storage and blobs](./storage-and-blobs.md): choose SQLite or Postgres, configure local
  disk or S3, and upgrade an existing data directory.
- [Release packaging](./release.md): understand package contents, build checks, and
  publication steps.
- [Changelog](../CHANGELOG.md): review changes by release.
- [Release checklist](./release-checklist.md): follow the release owner's publication
  checks.

## Contract files

The guides describe behavior in plain language. These files define the exact wire and
semantic contract:

- [`proto/earshot/v1alpha1/incident.proto`](../proto/earshot/v1alpha1/incident.proto)
- [`semconv/earshot.yaml`](../semconv/earshot.yaml)
- [`spec/incident-bundle.schema.json`](../spec/incident-bundle.schema.json)
- [`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json)
