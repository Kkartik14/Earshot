# Earshot documentation

These guides explain how to run Earshot, send it evidence, and understand what it
stores. Start with the [project README](../README.md) for the quick start and a
summary of supported inputs. Read [Architecture](./architecture.md) for the data
flows, then choose a guide below for the task you have.

## Run and operate Earshot

- [OTLP quick start](./otlp-quickstart.md): send spans, upload recordings, and read
  Calls.
- [Storage and blobs](./storage-and-blobs.md): choose SQLite or Postgres, configure
  local disk or S3, and upgrade an existing data directory.
- [Production notes](./production.md): plan the server, database, proxy, backups, and
  retention.
- [Server configuration](./configuration.md): configure `earshot serve` and the ASGI
  app.
- [Privacy](./privacy.md): choose a Capture Preset and understand retention, purge,
  and erasure limits.

## Integrate an application

- [Python SDK and recorder](./sdk.md): record a session in-process, use adapters, and
  export an Incident.
- [Browser SDK](../packages/browser/README.md): capture browser media and network
  measurements and send them to Earshot.
- [Backend API](./backend-api.md): send evidence, read stored Incidents, follow live
  sessions, and manage projects.
- [Hosted-provider connectors](./connectors.md): understand signature checks,
  normalization, and provider evidence limits.
- [Pipecat adapter](./adapters/pipecat.md), [LiveKit adapter](./adapters/livekit.md),
  and [TVIC runtime adapter](./adapters/tvic.md): see what each integration records.
- [Raw provider adapters](./adapters/providers.md): map Deepgram, Cartesia, OpenAI
  Realtime, and Sarvam events into an Incident.
- [Pipecat provider matrix](./pipecat-provider-matrix-2026-10-20.md): review live
  compatibility evidence and its limits.

## Understand the data model

- [Incident bundle](./incident-bundle.md): learn the portable artifact format.
- [Semantic conventions](./semantic-conventions.md): read the operation, event,
  link, and evidence vocabulary.
- [Deterministic analysis](./analysis.md): see how Earshot builds projections and
  evidence-linked findings.
- [Storage internals](./storage.md): understand the index, object store, and cleanup
  jobs.
- [Retained real captures](./captured-fixtures.md): review the public artifact
  inventory and the rules for keeping source captures private.
- [Conformance](./conformance.md): find the fixtures and release checks that protect
  compatibility.
- [Architecture decisions](./adr/): read why key design choices were made.

## Develop and release

- [Development](./development.md): set up the repository, run the application, and
  update generated contracts.
- [Release packaging](./release.md): understand package contents, build checks, and
  publication steps.
- [Changelog](../CHANGELOG.md): review changes by release.

## Contract files

The guides describe behavior in plain language. The protocol definition, semantic
conventions, generated schemas, and shared fixtures define the exact wire contract:

- [`proto/earshot/v1alpha1/incident.proto`](../proto/earshot/v1alpha1/incident.proto)
- [`semconv/earshot.yaml`](../semconv/earshot.yaml)
- [`spec/incident-bundle.schema.json`](../spec/incident-bundle.schema.json)
- [`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json)
- [`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json)
- Shared valid and invalid fixtures under [`fixtures/`](../fixtures/)

If prose and a machine-readable contract disagree, the contract and its
conformance fixtures define what the implementation accepts.
