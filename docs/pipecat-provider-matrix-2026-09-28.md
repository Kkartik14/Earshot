# Pipecat provider matrix — 2026-09-28

This is the live Cartesian-product run for the Pipecat path used by TVIC. The
tracked runner is [`scripts/run_pipecat_matrix.py`](../scripts/run_pipecat_matrix.py);
the machine-readable result, including incident hashes and unavailable cells, is
[`pipecat-provider-matrix-2026-09-28.json`](pipecat-provider-matrix-2026-09-28.json).

Run it with real credentials using:

```bash
set -a && source .env && set +a
.venv/bin/python scripts/run_pipecat_matrix.py --force
```

The dimensions are the TVIC catalog as of this run:

- STT: Deepgram `nova-3`, Cartesia STT `ink-2`, Sarvam `saaras:v3`, ElevenLabs
  `scribe_v2_realtime`, AssemblyAI `u3-rt-pro`, Soniox `stt-rt-v5`.
- LLM: Groq `openai/gpt-oss-20b`, OpenAI `gpt-4.1-mini`.
- TTS: Cartesia `sonic-3.6`, ElevenLabs `eleven_flash_v2_5`.

That is 6 × 2 × 2 = 24 cells. Sixteen ran against real providers; eight were
explicitly unavailable because the corresponding API key was not present. No
unavailable cell was replaced with a synthetic pass.

| STT | LLM | TTS | Result | Evidence |
| --- | --- | --- | --- | --- |
| deepgram | groq | cartesia | completed | valid Earshot incident |
| deepgram | groq | elevenlabs | timed out | `provider_http_402`, TTS |
| deepgram | openai | cartesia | timed out | `provider_http_429`, LLM |
| deepgram | openai | elevenlabs | timed out | `provider_http_429`, LLM |
| cartesia STT | groq | cartesia | completed | valid Earshot incident |
| cartesia STT | groq | elevenlabs | timed out | `provider_http_402`, TTS |
| cartesia STT | openai | cartesia | timed out | `provider_http_429`, LLM |
| cartesia STT | openai | elevenlabs | timed out | `provider_http_429`, LLM |
| sarvam | groq | cartesia | completed | valid Earshot incident; 16 kHz input |
| sarvam | groq | elevenlabs | timed out | `provider_http_402`, TTS |
| sarvam | openai | cartesia | timed out | `provider_http_429`, LLM |
| sarvam | openai | elevenlabs | timed out | `provider_http_429`, LLM |
| elevenlabs STT | groq | cartesia | completed | valid Earshot incident |
| elevenlabs STT | groq | elevenlabs | timed out | `provider_http_402`, TTS |
| elevenlabs STT | openai | cartesia | timed out | `provider_http_429`, LLM |
| elevenlabs STT | openai | elevenlabs | timed out | `provider_http_429`, LLM |
| assemblyai | groq | cartesia | unavailable | missing `ASSEMBLYAI_API_KEY` |
| assemblyai | groq | elevenlabs | unavailable | missing `ASSEMBLYAI_API_KEY` |
| assemblyai | openai | cartesia | unavailable | missing `ASSEMBLYAI_API_KEY` |
| assemblyai | openai | elevenlabs | unavailable | missing `ASSEMBLYAI_API_KEY` |
| soniox | groq | cartesia | unavailable | missing `SONIOX_API_KEY` |
| soniox | groq | elevenlabs | unavailable | missing `SONIOX_API_KEY` |
| soniox | openai | cartesia | unavailable | missing `SONIOX_API_KEY` |
| soniox | openai | elevenlabs | unavailable | missing `SONIOX_API_KEY` |

Every live cell produced a valid incident, including failures. Failed cells now
contain a metadata-only `provider_failure` operation with the provider, stage,
and status-only error type; response bodies, prompts, audio, transcripts, and
credentials are not retained. OpenAI returned HTTP 429 in the direct boundary
probe, and ElevenLabs returned HTTP 402. Those are provider boundaries, not
silently simulated runtime results.

## Latency policy

The 250 ms review bar is formally waived for remote provider response time in
this matrix: provider/network TTFB is evidence, not Earshot observer overhead.
The run measured Pipecat LLM TTFB at 557.5–6042.4 ms and TTS TTFB at
101.9–113.5 ms. The report therefore does not claim an end-to-end voice SLA of
under 250 ms. A product SLA at that level must be evaluated as a separate
provider/network budget; the Earshot integration remains non-blocking and
records those delays without hiding them.
