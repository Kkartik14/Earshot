# Raw provider adapters

Status: provider-SDK-free event mappers for custom, in-app voice pipelines.

Use these adapters when the application owns the WebSocket or SDK loop instead of
running LiveKit or Pipecat. They translate plain provider event dictionaries into
the same Incident contract consumed by Earshot analysis and storage. They perform
no network I/O and import no provider SDK.

```python
import earshot
from earshot.adapters.providers import DeepgramAdapter

adapter = DeepgramAdapter(model="nova-3")
session = earshot.pipeline(session_id="voice-session-42")

with session.turn() as turn:
    update = adapter.adapt(deepgram_message, received_at_ms=monotonic_ms())
    update.apply(turn)

incident = session.close()
```

`received_at_ms` and Cartesia's `request_sent_at_ms` are turn-relative values from
one application-monotonic clock. Do not pass Unix time or provider media offsets.
`AdapterUpdate.apply()` is idempotent for an exact update. `correlation_id` is an
opaque keyed digest; raw request, response, context, transcript, and audio values
are never retained.

## Evidence rules

- Provider media offsets and processing scalars remain provider-native
  measurements with their documented units and boundaries.
- Application send/receipt deltas use `source="app"` and estimated confidence.
- No adapter promotes a word timestamp or server media-buffer offset into network
  TTFB.
- First received audio is not client render. `client.render` remains unobserved.
- Provider interruption detection is not accepted barge-in. The application must
  explicitly author acceptance after output cancellation or playout stop.
- Realtime speech-to-speech remains one `agent` operation; Earshot does not invent
  internal STT, LLM, or TTS stages.

## Supported mappings

| Adapter                 | Native input                                                                        | Retained Earshot facts                                                                                                      |
| ----------------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `DeepgramAdapter`       | `Results`, `SpeechStarted`, `UtteranceEnd`                                          | STT stage; segment/cursor measurements; final transcript; natural turn commit; forced-final marker                          |
| `CartesiaAdapter`       | `chunk`, `done`, `error`                                                            | Per-chunk `step_time`; app request-to-first-chunk TTFB; first received audio; terminal/error metadata                       |
| `OpenAIRealtimeAdapter` | speech start/stop, input transcription completion, response create/audio delta/done | Fused `agent` interval; app speech-stop-receipt to first-audio-receipt latency; interruption detection; terminal status     |
| `SarvamAdapter`         | `events`, transcription `data`, `error`                                             | VAD receipt boundaries; native audio/processing measurements; final transcript; language code/probability; failed STT stage |

Deepgram's `start`, `duration`, and `last_word_end` are audio-stream coordinates,
not precise transport latency. Cartesia's `step_time` is per-chunk server
processing, not TTFB. Sarvam's `processing_latency` is documented in seconds and
is converted exactly once to milliseconds. OpenAI Realtime's `audio_end_ms` is a
session media-buffer offset; response latency therefore uses local receipt times
on both sides.

Sarvam language is projected from STT operations into `TurnFact.language` and can
be queried with `GET /v1/metrics/turns?...&group_by=language`. Missing or
conflicting STT language values become the `unknown` group; they are never guessed.

## Advanced facade

Custom adapters can use the same lower-level seam directly:

```python
operation_id = turn.record_stage("agent", "provider", model="speech-model")
turn.record_measurement(
    "earshot.turn.response_latency",
    410,
    unit="ms",
    operation_id=operation_id,
    source="app",
    confidence="estimated",
    basis="vad_stop_receipt_to_first_audio_receipt",
)
turn.record_event(
    "earshot.audio.first_packet_received",
    at_ms=1410,
    participant="agent",
    source="app",
    confidence="estimated",
)
```

Each fact carries independent evidence. A provider-measured scalar can therefore
coexist with an application-estimated arrival boundary without making the whole
stage look provider-measured.

## Verification

```bash
.venv/bin/pytest -q \
  packages/sdk-python/tests/test_pipeline_facade.py \
  packages/sdk-python/tests/test_provider_adapters_deepgram_cartesia.py \
  packages/sdk-python/tests/test_provider_adapters_openai_sarvam.py
```

Core tests use minimal, synthetic, content-bearing payloads and assert that content
does not reach canonical incidents. Optional real-provider checks must keep keys in
environment variables and store only scrubbed, gitignored captures.

Primary references: [Deepgram streaming STT](https://developers.deepgram.com/reference/speech-to-text/listen-streaming),
[Cartesia WebSocket TTS](https://docs.cartesia.ai/api-reference/tts/websocket),
[OpenAI Realtime WebSocket lifecycle](https://developers.openai.com/api/docs/guides/realtime-conversations#handling-audio-with-websockets),
and [Sarvam streaming STT schema](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/ws.md).
