# LiveKit examples instrumented with Earshot

These examples exercise Earshot against the installed LiveKit Agents 1.6.x
package using LiveKit's own OpenTelemetry spans and metrics. Each script reads
the installed distribution version for artifact provenance; no version string
is hard-coded into captured evidence. Earshot adds a span processor and metrics
listeners; it does not replace LiveKit's trace root.

The examples use explicit `whisper-1`, `gpt-4o-mini`, and `tts-1` model names
for reproducibility. They are example choices, not a claim that newer model
families are unavailable. Check the current OpenAI and LiveKit model
documentation before choosing production models.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,livekit]'
pip install 'livekit-agents[openai,silero,turn-detector]~=1.6.5'
python examples/livekit_console/agent.py download-files
export OPENAI_API_KEY=sk-...
```

The `download-files` step fetches the Silero VAD and optional multilingual turn
detector assets.

## Choose a driver

### Real console conversation

```bash
python examples/livekit_console/agent.py console
```

This is a genuine microphone → STT → LLM → TTS → speaker conversation. LiveKit
console mode is local and does not require LiveKit server credentials. End the
call with `Ctrl-C` or `q`; the async shutdown hook waits for AgentSession to
close and flushes its trace provider before Earshot finalizes the incident.

The artifact is written to:

```text
.earshot/livekit_console/console_incident.json
```

### Headless text turn

```bash
python examples/livekit_console/drive_once.py
```

This sends text through LiveKit's evaluation harness and exercises a real LLM
and TTS call. A contract-compliant null output sink receives synthesized audio,
so this is not merely an LLM text assertion. It does **not** exercise STT, VAD,
or endpointing: setting an input modality does not turn a text string into
microphone audio.

The artifact is written to:

```text
.earshot/livekit_console/text_incident.json
```

### Headless full audio pipeline

```bash
python examples/livekit_console/drive_audio.py
```

This first synthesizes a WAV utterance, then sends its audio frames through a
roomless AgentSession. It exercises VAD, real STT, endpointing, LLM, and TTS
without a microphone. The generated files are:

```text
.earshot/livekit_console/user_utterance.wav
.earshot/livekit_console/audio_incident.json
```

The WAV contains the synthesized user utterance. It is intentionally kept
under the gitignored `.earshot/` directory; treat it as governed audio data.

## Expected output

Counts vary with conversation length and LiveKit's emitted spans. A console run
prints a summary similar to:

```text
====================================================================
EARSHOT INCIDENT (real LiveKit call)
====================================================================
  session_id     : console-session
  operations     : 8
  events         : 6
  quality_samples: 12
  turns          : 2
  stages seen    : llm, stt, tts, turn_detection
  turn a1b2...    response=740 ms       first_token=310 ms
  ...
```

VAD is a continuous signal recorded in `pipeline.metric` quality samples, so it
is not expected in the operation-derived `stages seen` line.

## Validate or ingest an artifact

Set `ARTIFACT` to any of the three incident paths above:

```bash
ARTIFACT=.earshot/livekit_console/console_incident.json
python -c "import pathlib; from earshot.codec import decode_incident_json; \
from earshot.validation import validate_incident; \
b=decode_incident_json(pathlib.Path('$ARTIFACT').read_bytes()); \
r=validate_incident(b); print('valid:', r.ok, '| errors:', len(r.errors))"

# In another shell:
uvicorn apps.ingest.app:app --port 4319

curl -s -X POST localhost:4319/v1/incidents \
  -H 'content-type: application/vnd.earshot.incident+json' \
  --data-binary "@$ARTIFACT"
```

## What these examples prove

- The console and audio drivers exercise real audio input, LiveKit framework
  telemetry, and the configured external models.
- The text driver isolates the LLM/TTS path and is useful when audio input is
  unnecessary.
- Each driver produces a contract-valid incident that can be analyzed or sent
  to the local ingest API.
- Incident capture is metadata-only by default: transcript and audio payloads
  are not embedded in the incident unless a broader `CapturePolicy` opts in.
- Client-render evidence remains `not_observed` with reason
  `server_cannot_observe_client_render` until a browser or mobile collector
  supplies it.

Every run makes billable model API calls. Review provider pricing and use a
dedicated development project or spending limit when experimenting.

The headless drivers write an incident even when model startup, synthesis,
session shutdown, or trace flushing fails. Their session status is then
`failed` (or `timed_out` for a clean timeout), and they return a non-zero exit
code. Exit zero additionally requires a contract-valid artifact containing the
expected real pipeline stages; the audio driver also requires final STT and
assistant transcripts plus a VAD inference metric.
