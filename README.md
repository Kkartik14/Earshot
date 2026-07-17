# Earshot

**Portable, evidence-labelled incident artifacts for real-time voice AI sessions.**

Earshot is an open-source SDK, semantic profile, and local backend for recording what
happened in a voice session—across Pipecat, LiveKit, browser/mobile, native
speech-to-speech, raw pipelines, and optional telephony—and turning it into a safe,
immutable artifact that can be validated, stored, shared, and projected into a
deterministic latency/causality analysis.

The core workflow:

> This session failed. Here is the portable evidence artifact and the deterministic
> projection needed to investigate it across runtimes.

Status: alpha M1 backend. Incident-to-explanation automation and regression-fixture
generation are later product milestones, not current implementation claims.

## What is implemented

- A v1 voice-session contract with participants, streams, distributed clock domains,
  graph causality, explicit coverage, evidence/provenance, privacy policy, media refs,
  and optional exact caller-supplied raw OTLP chunks. Automatic OTLP interception is
  not implemented in M1.
- Deterministic protobuf plus strict JSON codecs and generated JSON Schema.
- Bundle-wide invariant and privacy validation with stable issue codes.
- Metadata-only capture policy and omission ledger.
- Framework-neutral recorder, bounded fail-open exporter, and Pipecat/LiveKit
  normalization adapters.
- Deterministic analysis separating generated/sent/received/render timing, cross-clock
  uncertainty, parallel tool work, provider measurements, and measured failed-operation
  diagnoses. Broader fault explanation remains roadmap work.
- FastAPI backend with project-scoped API keys, immutable SQLite/content-addressed
  storage, JSON/protobuf negotiation, fleet Turn Facts, analysis caching, corruption
  checks, and privacy purge/tombstones.
- Signed finalized-delivery Connectors for ElevenLabs Agents (JSON and OTLP-shaped),
  Vapi, and Retell. Provider transcript/tool/dynamic-variable bodies are not retained.
- A non-root single-image deployment with a persistent `/data` volume and hardened
  Compose example.
- Unit, property, integration, smoke, and end-to-end conformance tests.

## Quick start

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
earshot serve --data-dir .earshot
```

The API starts at `http://127.0.0.1:4319`.

For the container path:

```bash
export EARSHOT_TOKEN="$(openssl rand -hex 32)"
docker compose up --build
```

Compose publishes only to `127.0.0.1:4319` and persists the catalog and evidence in the
named `earshot-data` volume.

The Python distribution is named `earshot-observability`; the import package and
CLI remain `earshot`. The plain PyPI distribution name `earshot` belongs to an
unrelated VAD project and must not be used for this repository.

```bash
curl http://127.0.0.1:4319/healthz
earshot validate fixtures/valid/minimal.json
earshot ingest fixtures/valid/minimal.json --data-dir .earshot
```

## Architecture

```text
framework/runtime facts
  -> capture-policy filter
  -> existing OTel graph + earshot.* voice profile
  -> canonical incident bundle (protobuf; JSON debug export)
       -> local validation + immutable storage
       -> deterministic projection and evidence-linked diagnosis
       -> governed local API/CLI or portable file export
```

The application's normal OpenTelemetry exporter may continue sending telemetry to an
existing backend in parallel. Earshot accepts ElevenLabs' bounded, finalized OTLP-shaped
webhook, but does not expose a generic live OTLP receiver: OTLP alone does not define
voice-session completion, late-span revision, or multi-trace correlation.

The voice boundary is capture through render. Transport is optional evidence—not the
data-model center. Native speech-to-speech can legitimately omit STT/LLM/TTS stages.
Missing evidence is never encoded as zero.

Earshot distinguishes audio generated, sent, received, and rendered. It never claims
a system can prove a human **heard** the audio.

## Repository

| Path                                   | Purpose                                                           |
| -------------------------------------- | ----------------------------------------------------------------- |
| `proto/earshot/v1`                     | Canonical protobuf envelope.                                      |
| `semconv/earshot.yaml`                 | Earshot OTel semantic-profile registry.                           |
| `spec/`                                | Generated JSON Schema.                                            |
| `packages/sdk-python`                  | Contract, SDK, adapters, analysis, storage, and API.              |
| `apps/ingest`                          | ASGI deployment entry point.                                      |
| `fixtures/`                            | Shared valid/invalid/golden/fault artifacts.                      |
| `examples/pipecat_headless`            | Roomless real STT → LLM → TTS evidence harness.                   |
| `docs/`                                | Public, self-reproducing architecture and contract documentation. |
| `packages/schema`, `packages/analysis` | Superseded M0 TypeScript prototype.                               |

Start with the [public documentation](docs/README.md).

## Non-goals

Earshot is not a voice runtime, carrier, hosted multi-tenant product, or another fleet
dashboard/evaluation suite. It creates the portable artifact those systems can ingest.
