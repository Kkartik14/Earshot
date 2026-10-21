# LiveKit → OTLP example

One real LiveKit Agents 1.8 call, headless (scripted WAV caller → Silero VAD → Deepgram → Groq → Cartesia → a speaker that plays at real time), traced over OTLP/HTTP and recorded by LiveKit's `RecorderIO`. No LiveKit server: the entrypoint runs as an in-process job (`AgentServer.simulate_job(fake_job=True)`).

1. In a separate venv: `pip install -r examples/livekit_otlp/requirements.txt`. Export `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` (optional: `GROQ_MODEL`, `CARTESIA_MODEL`); leave `LIVEKIT_URL` unset.
2. `export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`. No receiver yet? Run `python scripts/otlp_capture_sink.py`.
3. From the repo root: `python examples/livekit_otlp/run_call.py --out .earshot/livekit_otlp/run`. The caller WAV is shared with `examples/pipecat_otlp` and cached in `.earshot/pipecat_otlp/`.
4. `--out` gets `recording.ogg` (stereo Ogg/Opus: channel 0 user, channel 1 agent; sample 0 at `recording.started_at`), `session_report.json` and `manifest.json` (written at session start, completed after the flush). Every span carries `earshot.session.id` (`earshot.client.tracer_metadata`).
5. `--upload [BASE_URL]` (default: the OTLP endpoint; key from `EARSHOT_API_KEY` or the OTLP headers) creates the call's recording in Earshot at session start (`speaker: mixed`, `channel_speakers: ["user", "agent"]`), so the call waits for its audio, and uploads `recording.ogg` from `on_session_end` with `earshot.client`.
