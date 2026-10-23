# Earshot

**See every call your voice agent makes.**

Earshot is a self-hosted server for voice-agent observability. Your agent sends its
OpenTelemetry traces and its call recordings to Earshot. Earshot groups them into Calls
and keeps each Call's timing, transcript and audio in one place, on your own machines. It
runs on a laptop with SQLite and local disk, or on a team server with Postgres and S3.

Status: pre-v1 alpha. 0.2.0 is the first release that receives OTLP traces and
recordings. Compatibility is limited to the versions documented for each release; see the
[alpha compatibility policy](docs/release.md#alpha-compatibility-policy).

## Quick start

This takes about a minute with a Pipecat 1.5 bot you already run.

**1. Install and start Earshot.**

```bash
pip install 'earshot-observability[server]'
earshot serve
```

Earshot listens on `http://127.0.0.1:4319` and keeps everything in `./.earshot`: a SQLite
catalog and a local blob directory. On loopback it needs no API key.

**2. Point Pipecat's HTTP OTLP exporter at it.** Use the HTTP exporter, not the gRPC one.

```bash
pip install 'pipecat-ai[tracing]>=1.5,<1.6' opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319
```

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from pipecat.pipeline.worker import PipelineWorker
from pipecat.utils.tracing.setup import setup_tracing

setup_tracing(service_name="my-voice-agent", exporter=OTLPSpanExporter())

worker = PipelineWorker(
    pipeline,  # your pipeline
    enable_tracing=True,
    conversation_id="call-123",
    additional_span_attributes={"earshot.session.id": "call-123"},
)
```

Set `conversation_id` and `earshot.session.id` to the **same** value, your own ID for the
call. That value is the Session Key: Earshot groups the call's spans by it, and the
Recordings API takes it. Pipecat 1.5 puts `conversation.id` on every `turn` span, and
those arrive first. It puts `earshot.session.id` only on the `conversation` span, which
it sends last, and a trace keeps the first key Earshot sees in it. So if the two values
differ, the Call is keyed by `conversation_id`, and a Recording created with the other
value becomes a second Call. This is a known limitation of 0.2.0; see
[session keys](docs/otlp-quickstart.md#session-keys).

**3. Upload the recordings.** Create each Recording when the call starts, at its first
audio frame, and upload its bytes after the call ends: one mono file per speaker.

```bash
# When the call starts:
curl -X POST http://127.0.0.1:4319/v1/recordings \
  -H 'Content-Type: application/json' \
  -d '{"recording_id": "call-123-user", "session_key": "call-123", "speaker": "user",
       "content_type": "audio/wav", "started_at_unix_nano": "1792372161387965000"}'
# After the call ends:
curl -X PUT http://127.0.0.1:4319/v1/recordings/call-123-user/content \
  -H 'Content-Type: audio/wav' --data-binary @user.wav
```

A pending Recording holds its Call for up to 10 minutes after the latest of its creation,
the Call's latest evidence, and its end signal. If no upload is streaming by then, Earshot
marks it failed. A PUT already in flight has a separate 10-minute deadline measured from
upload start, so it can keep the Call open beyond the pending deadline. A Recording
created only after the Call has closed appears in a later `late_data` Revision, about 30
seconds after the upload.

**4. See the call.**

```bash
curl http://127.0.0.1:4319/v1/calls
```

A Call is listed as soon as its first Recording is created or its first span arrives.
Pipecat sends its `conversation` span as an end signal. Earshot creates a final Revision
after a 10-second grace period and any active recording uploads finish or reach their
deadline. Without an end signal, five quiet minutes make a provisional Revision due, but
a pending Recording can hold it until the deadlines above. `GET /v1/calls/<call_id>` shows
the Call and its Revisions.

The [OTLP quick start](docs/otlp-quickstart.md) covers API keys, containers, session keys,
close timing, recordings and error handling. [`examples/pipecat_otlp`](examples/pipecat_otlp/)
is a complete, real Pipecat call.

## What Earshot receives today

| Source                                      | Status in 0.2.0                                                                                                                                                                                          |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pipecat 1.5 over OTLP/HTTP                  | Supported: Calls with timing, transcript and recordings. Tested against a real Pipecat 1.5.0 call.                                                                                                       |
| Any OTLP/HTTP producer                      | Spans are stored and grouped into Calls by session key. A Call's Revision includes only Pipecat spans in 0.2.0; spans from other instrumentation stay stored and are declared as omitted.                |
| LiveKit Agents over OTLP/HTTP               | Partial: Earshot recognizes LiveKit's session key and its end-of-session spans, so Calls form and close. LiveKit spans are not mapped into Revisions yet; full LiveKit support is the next planned step. |
| Earshot Python SDK                          | Supported: Pipecat 1.6 and LiveKit 1.6 adapters, and Deepgram, Cartesia, Sarvam and OpenAI Realtime event mappers, recorded as portable Incident bundles.                                                |
| ElevenLabs, Vapi, Retell, Ringg             | Signed webhook Connectors for finalized deliveries. Synthetic conformance only.                                                                                                                          |
| Browser (`@earshot/browser`, `/v1/capture`) | Supported for browser telemetry and continuous capture. Validated on Chrome only. See the browser SDK README for tested behavior and limits.                                                       |

gRPC, stereo recordings and a call view in the viewer are not in 0.2.0. Each closed
Call's Revision is an ordinary Incident, so the bundled viewer and the analysis APIs can
open it.

## Capture presets

Each Project has a Capture Preset. The server applies it to spans and recordings before it
stores them.

| Preset          | Stores                                                                                                                                                                  |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `full`          | Timing, transcripts, model and tool payloads, the session key, and recordings. The default for every Project in a new 0.2.0 install.                                    |
| `metadata_only` | Timing and structure only. Content, identity and the session-key attributes are removed before storage, and recordings are refused. Upgraded 0.1.x Projects start here. |
| `redacted`      | Not available in 0.2.0.                                                                                                                                                 |

```bash
earshot project set-capture --project default --preset metadata_only
earshot project show default
```

[Privacy](docs/privacy.md) lists exactly what each preset keeps, and how purge, retention
and erasure work.

## Storage

|            | Laptop (default)               | Team server                                                                                   |
| ---------- | ------------------------------ | --------------------------------------------------------------------------------------------- |
| Catalog    | SQLite in `--data-dir`         | Postgres: `EARSHOT_DATABASE_URL=postgresql://…`, the `[postgres]` extra and a correlation key |
| Recordings | Local disk, `<data-dir>/blobs` | S3 or an S3-compatible store: `EARSHOT_BLOB_URL=s3://…` and the `[s3]` extra                  |

```bash
pip install 'earshot-observability[server,postgres,s3]'
```

`earshot serve` migrates the schema on start. `earshot db current` and `earshot db upgrade`
report and apply migrations. See [storage and blobs](docs/storage-and-blobs.md) and the
[production notes](docs/production.md): one server per database, one worker, and a
Collector in front.

## Run it in a container

```bash
docker compose up --build
# then open http://127.0.0.1:4319
```

The image includes the `server`, `postgres` and `s3` extras. Compose publishes the port
on `127.0.0.1` only, keeps data in the `earshot-data` volume, and runs with
`EARSHOT_TRUST_LOCAL_NETWORK=true`, so the API is unauthenticated behind that loopback
mapping. A bot in another container needs an API key; see the
[OTLP quick start](docs/otlp-quickstart.md#api-keys-and-the-host-rule). To expose Earshot
beyond localhost, drop that line, set `EARSHOT_TOKEN` or issue API keys, set
`EARSHOT_BEHIND_TLS_PROXY=true`, and put your own TLS proxy in front.

## Also in the box

- **A portable evidence format.** The `v1alpha1` Incident bundle: deterministic protobuf
  and strict JSON, a semantic profile, privacy validation and an omission ledger.
- **A Python SDK** that records Incidents in-process (`earshot.init()`), with bounded,
  non-blocking delivery and crash recovery. See [the SDK](docs/sdk.md).
- **Deterministic analysis** of each Incident: generated, sent, received and rendered
  audio timing, cross-clock uncertainty and evidence-linked diagnoses. Earshot never
  claims a person heard the audio.
- **A viewer** served by `earshot serve`, and `@earshot/viewer-ui` for hosts that embed it.
- **Hosted mode** with short-lived, project-scoped JWTs (`EARSHOT_AUTH_MODE=hosted_jwt`).
  The call, recording and OTLP routes are not available in hosted mode in 0.2.0.

The Python distribution is `earshot-observability`; the import package and the CLI are
`earshot`. The PyPI project named `earshot` is an unrelated VAD package. The base install
(`pip install earshot-observability`) is the SDK only; every command that opens the store,
`earshot serve` included, needs the `server` extra.

## Develop

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
pnpm install && pnpm --filter @earshot/viewer bundle   # build the UI into the package
earshot serve --data-dir .earshot
```

See [development](docs/development.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [OTLP quick start](docs/otlp-quickstart.md): exporter setup, session keys, close timing,
  recordings, errors.
- [Privacy](docs/privacy.md): capture presets, purge, retention and erasure.
- [Storage and blobs](docs/storage-and-blobs.md): SQLite or Postgres, migrations,
  upgrading from 0.1.x, local disk or S3.
- [Production notes](docs/production.md): topology, Collector, readiness, backups.
- [Server configuration](docs/configuration.md): every variable.
- [All documentation](docs/README.md) and the [changelog](CHANGELOG.md).

## Non-goals

Earshot is not a voice runtime, a carrier, or a hosted multi-tenant product.

## License

Apache-2.0. See [LICENSE](LICENSE).
