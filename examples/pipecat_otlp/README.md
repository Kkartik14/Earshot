# Pipecat → OTLP example

One real Pipecat 1.5 call (scripted WAV caller → Silero VAD → Deepgram → Groq → Cartesia), traced over OTLP/HTTP.

1. `pip install -r examples/pipecat_otlp/requirements.txt`, then export `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` (optional: `GROQ_MODEL`, `CARTESIA_MODEL`).
2. Point the standard exporter at a receiver: `export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` (`OTEL_EXPORTER_OTLP_HEADERS` for auth). No receiver yet? Run `python scripts/otlp_capture_sink.py`.
3. From the repo root: `python examples/pipecat_otlp/run_call.py --out .earshot/pipecat_otlp/run`. The caller WAV is synthesized once and cached in `.earshot/pipecat_otlp/`.
4. `--out` gets `user.wav`, `agent.wav` (16 kHz mono), `call.wav` (both as one stereo file: channel 0 user, channel 1 agent) and `manifest.json`; `started_at_unix_nano` is the input's first audio frame, sample 0 of every track. `--upload [BASE_URL]` (Earshot ≥0.3.0, `earshot.client`; default: the OTLP endpoint; key from `EARSHOT_API_KEY` or the OTLP headers) creates the stereo recording (`channel_speakers: ["user", "agent"]`) at first audio and uploads `call.wav` after the call.
5. Known Pipecat behaviour: when audio frames reach `AudioBufferProcessor` more than 200 ms late and then in a burst, it pads the gap with silence, so both tracks drift later from that point. Earshot corrects for it in Month 2.
