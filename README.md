# Earshot

**The open-source Voice Observability SDK.**

Earshot is an open-source SDK, semantic profile, and local backend for recording what
happened in a voice session—across Pipecat, LiveKit, native speech-to-speech event
streams, raw provider pipelines, and optional telephony—and turning it into a safe,
immutable artifact that can be validated, stored, shared, and projected into a
deterministic latency/causality analysis. A browser capture SDK and its ingest endpoint
exist but are unpublished and validated on one browser only; the viewer does not yet
render browser-captured sessions, and render collection — confirming client playback
actually occurred — is not shipped.

The core workflow:

> This session failed. Here is the portable evidence artifact and the deterministic
> projection needed to investigate it across runtimes.

Status: pre-v1 alpha. Public compatibility is limited to the versions documented for
each release; see the [alpha compatibility policy](docs/release.md#alpha-compatibility-policy).

| Maturity                                           | Surfaces                                                                                                                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Implemented; retained real-capture fixture         | LiveKit 1.6, Pipecat 1.5, Deepgram, Cartesia, and Sarvam adapters                                                                                                              |
| Implemented; synthetic conformance only            | OpenAI Realtime event mapper and ElevenLabs, Vapi, Retell, and Ringg finalized-delivery Connectors                                                                             |
| Implemented core                                   | Contract/codecs, recorder/export, privacy validation, local API/storage, deterministic analysis/explanation, and generic viewer                                                |
| Implemented; unpublished, single-browser validated | Browser capture SDK (`packages/browser`) and its `/v1/capture` ingest endpoint                                                                                                 |
| Planned; not available in the current release      | Viewer rendering of browser-captured sessions, render collection (confirming client playback), generic live OTLP receiving, media upload/replay, and regression-fixture export |

The retained artifacts are metadata-only and privacy-scrubbed; their provenance and
current gaps are recorded in [the capture inventory](docs/captured-fixtures.md).

## What is implemented

- A pre-v1 `v1alpha1` voice-session contract with participants, streams, distributed clock domains,
  graph causality, explicit coverage, evidence/provenance, privacy policy, media refs,
  and optional exact caller-supplied raw OTLP chunks. Automatic OTLP interception is
  not implemented in M1.
- Deterministic protobuf plus strict JSON codecs and generated JSON Schema.
- Bundle-wide invariant and privacy validation with stable issue codes.
- Metadata-only capture policy and omission ledger.
- Explicit and global SDK clients with context ownership, deterministic whole-conversation
  sampling, lifecycle/loss status, fork/atexit recovery, bounded gzip export, and
  Pipecat/LiveKit normalization adapters.
- Deterministic analysis and backend explanation separating generated/sent/received/render timing, cross-clock
  uncertainty, parallel tool work, provider measurements, and measured failed-operation
  diagnoses. The viewer consumes authored points/intervals, provenance, coverage,
  omissions, and limitations without manufacturing durations.
- FastAPI backend with project-scoped API keys, immutable SQLite/content-addressed
  storage, JSON/protobuf negotiation, fleet Turn Facts, analysis caching, corruption
  checks, privacy purge/tombstones, and mutable project-scoped Platform/Voice Labs links.
- Shared `@earshot/viewer-ui` Observe feature mounted by the standalone Vite app and an
  App Router Next.js host; Platform supplies its own authenticated shell and API proxy.
- Signed finalized-delivery Connectors for ElevenLabs Agents (JSON and OTLP-shaped),
  Vapi, Retell, and Ringg. Provider transcript/tool/dynamic-variable bodies are not
  retained.
- A non-root single-image deployment with a persistent `/data` volume and hardened
  Compose example.
- Unit, property, integration, smoke, and end-to-end conformance tests.

## Quick start

Clone and run the whole thing — the API and the web viewer in one container:

```bash
docker compose up --build
# then open http://127.0.0.1:4319
```

That builds the viewer SPA, bakes it into the image, and serves the UI and the `/v1`
API from a single port. Compose publishes only to `127.0.0.1:4319` and persists the
catalog and evidence in the named `earshot-data` volume. The container runs with
`EARSHOT_TRUST_LOCAL_NETWORK=true`: the API is unauthenticated, but the loopback-only
port mapping is the trust boundary. To expose it beyond localhost, drop that line from
`compose.yaml` and instead set `EARSHOT_TOKEN` (a high-entropy secret) plus
`EARSHOT_BEHIND_TLS_PROXY=true`, and front it with your own TLS proxy.
The standalone viewer exchanges an entered project API key or legacy token for an
expiring HttpOnly session cookie; it never saves the credential in browser storage.
Hosted deployments use short-lived, project-scoped JWTs verified against the
configured issuer and HTTPS JWKS. Platform's token issuer stays server-side; its
backend-for-frontend (BFF) proxies authenticated detail and live/SSE requests to
Earshot. The optional Platform summary adapter is disabled by default and uses
the same project-scoped JWT authorization. The browser never receives an Earshot
service token or signing credential.

Load a session to look at:

```bash
curl -X POST http://127.0.0.1:4319/v1/incidents \
  -H 'Content-Type: application/json' --data-binary @fixtures/valid/minimal.json
```

### From source (no container)

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
pnpm install && pnpm --filter @earshot/viewer bundle   # build the UI into the package
earshot serve --data-dir .earshot                       # http://127.0.0.1:4319
```

Applications that only emit Earshot evidence install the lightweight base package.
Running the local API/CLI server from a non-development installation requires the
server extra: `pip install 'earshot-observability[server]'`.

For the standalone App Router host, start the API separately and run
`EARSHOT_API_URL=http://127.0.0.1:4319 pnpm --filter @earshot/viewer-next dev`.
The Python wheel and single-image Docker path continue to serve the packaged Vite static
viewer. See [Platform integration](docs/platform-integration.md) for the host auth,
reference-link, and TVIC artifact contract.

Without the `bundle` step the API still runs; it just serves no UI. During UI
development, run `pnpm --filter @earshot/viewer dev` for a hot-reloading server that
proxies `/v1` to the backend. Its default proxy target is `http://127.0.0.1:8000`, not
the CLI's own default port 4319, so either start the backend with `--port 8000` or set
`EARSHOT_API_URL=http://127.0.0.1:4319` before running `dev`.

The Python distribution is named `earshot-observability`; the import package and
CLI remain `earshot`. The plain PyPI distribution name `earshot` belongs to an
unrelated VAD project and must not be used for this repository.

Instrument an application through the environment-configured process client:

```python
import earshot

earshot.init()  # EARSHOT_ENDPOINT, EARSHOT_TOKEN, EARSHOT_PROJECT_ID, ...

with earshot.conversation(session_id="opaque-session-id") as incident:
    with incident.operation("agent", turn_id="turn-1"):
        run_voice_agent()

assert earshot.flush(timeout=5.0)
earshot.shutdown(timeout=5.0)
```

`earshot.Client(...)` provides the same capture kernel with explicit ownership for
libraries, tests, and multi-project processes. Async delivery is bounded and
non-blocking; sync and disk-durable delivery are explicit opt-in modes. `status()` makes
sampling, queue pressure, retries, rejection, and evidence loss observable.

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
| `proto/earshot/v1alpha1`               | Experimental canonical protobuf envelope.                         |
| `semconv/earshot.yaml`                 | Earshot OTel semantic-profile registry.                           |
| `spec/`                                | Generated JSON Schema.                                            |
| `packages/sdk-python`                  | Contract, SDK, adapters, analysis, storage, and API.              |
| `apps/ingest`                          | ASGI deployment entry point.                                      |
| `apps/viewer`                          | React viewer SPA, bundled into the Python wheel.                  |
| `apps/viewer-next`                     | Standalone Next.js App Router host for the same Observe feature.  |
| `packages/viewer-ui`                   | Earshot-owned framework-neutral Observe feature and API client.   |
| `packages/browser`                     | Browser-side capture SDK (unpublished; not yet viewer-rendered).  |
| `fixtures/`                            | Shared valid/invalid/golden/fault artifacts.                      |
| `examples/pipecat_headless`            | Roomless real STT → LLM → TTS evidence harness.                   |
| `docs/`                                | Public, self-reproducing architecture and contract documentation. |
| `packages/schema`, `packages/analysis` | Superseded M0 TypeScript prototype.                               |

Start with the [public documentation](docs/README.md).

## Non-goals

Earshot is not a voice runtime, carrier, hosted multi-tenant product, or another fleet
dashboard/evaluation suite. It creates the portable artifact those systems can ingest.
