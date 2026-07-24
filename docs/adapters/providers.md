# Raw provider adapters

Status: implemented pre-v1, provider-SDK-free event mappers for custom, in-app voice
pipelines. Deepgram, Cartesia, and Sarvam have
[privacy-scrubbed retained real captures](../captured-fixtures.md). The OpenAI Realtime
mapper is covered by synthetic conformance only until a real provider capture is
available.

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
are never retained. Apply each update before adapting the next stream event: parsing
alone never advances response, turn, or first-audio state.

## Evidence rules

- Provider media offsets and processing scalars remain provider-native
  measurements with their documented units and boundaries.
- Application send/receipt deltas use `source="app"` and estimated confidence.
- No adapter promotes a word timestamp or server media-buffer offset into network
  TTFB.
- First received audio is not client render. `client.render` remains unobserved.
- Provider interruption detection is not accepted barge-in. The application must
  explicitly author acceptance after output cancellation or playout stop. The
  Realtime adapter does this only when a speech-started response later ends cancelled.
- Realtime speech-to-speech remains one `agent` operation; Earshot does not invent
  internal STT, LLM, or TTS stages.
- Every discarded content field contributes a value-free privacy omission entry.

## Supported mappings

| Adapter                 | Native input                                                                                                                                                                                                                                       | Retained Earshot facts                                                                                                                                                   |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `DeepgramAdapter`       | Nova `Results`/cursor events; Flux v2 `TurnInfo` lifecycle                                                                                                                                                                                         | STT stage; segment/cursor measurements; speculative Flux transitions; final transcript; committed/forced finality                                                        |
| `CartesiaAdapter`       | `chunk`, word/phoneme timestamps, `done`, `error`                                                                                                                                                                                                  | Per-chunk `step_time`; app request-to-first-chunk TTFB; timestamp counts/durations; terminal/error metadata                                                              |
| `OpenAIRealtimeAdapter` | speech start/stop, transcription, response create/audio delta/audio done/done                                                                                                                                                                      | Fused `agent` interval; response-bound receipt latency; verified cancellation acceptance; terminal status                                                                |
| `GeminiLiveAdapter`     | `setupComplete`, `serverContent` (model turn/audio/transcription/interrupted/turn end), `toolCall`/`toolCallCancellation`, `usageMetadata`, `goAway`, `sessionResumptionUpdate`, and the client's own `realtimeInput`/`clientContent` turn signals | Fused `agent` interval; client-stop-to-first-audio receipt latency; verified barge-in acceptance; tool operations; per-modality `gen_ai.usage.*` tokens; terminal status |
| `SarvamAdapter`         | `events`, transcription `data`, `error`                                                                                                                                                                                                            | VAD receipt boundaries; native audio/processing measurements; final transcript; language code/probability; failed STT stage                                              |

Deepgram's `start`, `duration`, and `last_word_end` are audio-stream coordinates,
not precise transport latency. Cartesia's `step_time` is per-chunk server
processing, not TTFB. Sarvam's `processing_latency` is documented in seconds and
is converted exactly once to milliseconds. OpenAI Realtime's `audio_end_ms` is a
session media-buffer offset; response latency therefore uses local receipt times
on both sides.

For Flux, pass `agent_output_active=True` while adapting `StartOfTurn` only when
the application knows agent output is active. That condition authors interruption
detection, never acceptance. A single detected Flux language populates the fleet
dimension; multilingual turns retain a provider language count and project as
`unknown` rather than guessing one language. `response.output_audio.done` records
an output-part boundary but is not response-terminal; only `response.done` closes
the fused Realtime operation.

Sarvam language is projected from STT operations into `TurnFact.language` and can
be queried with `GET /v1/metrics/turns?...&group_by=language`. Missing or
conflicting STT language values become the `unknown` group; they are never guessed.
Configure `SarvamAdapter(language_code="unknown")` for auto-detection; a detected
BCP-47 language and probability are then accepted. A fixed configured language
rejects a provider probability as a schema conflict.

Gemini Live is a native speech-to-speech runtime like OpenAI Realtime: one model
turn projects into one fused `agent` operation, never invented STT, LLM, or TTS
stages. Gemini emits no server `speech_stopped` message and no per-response id, so
response latency is anchored on the client turn signal the application already owns
on its upstream half of the bidi socket (`realtimeInput.activityEnd` or
`clientContent.turnComplete`), and the fused response is correlated by an opaque
session-scoped id. First received audio (`serverContent.modelTurn.inlineData`)
authors `earshot.audio.first_packet_received` and the receipt-to-first-audio
`earshot.turn.response_latency`; it is never client render, so `client.render`
stays unobserved. Interruption acceptance is authored only when a real client
speech gesture (`realtimeInput.activityStart`) during an open response is later cut
off by a provider `serverContent.interrupted` signal — a bare `interrupted` frame
with no preceding gesture is detection, never acceptance. Per-modality
`usageMetadata` token counts reuse the canonical `gen_ai.usage.*` names, so a Gemini
session and a framework session aggregate identically, and `functionCalls` project
into `tool` operations with their arguments omitted.

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
  packages/sdk-python/tests/test_provider_adapters_openai_sarvam.py \
  packages/sdk-python/tests/test_provider_adapters_gemini.py \
  packages/sdk-python/tests/test_provider_adapter_parity.py
```

Core tests use minimal, synthetic, content-bearing payloads and assert that content
does not reach canonical incidents. Optional real-provider checks must keep keys in
environment variables and store only scrubbed, gitignored captures.

Primary references: [Deepgram streaming STT](https://developers.deepgram.com/reference/speech-to-text/listen-streaming),
[Deepgram Flux v2](https://developers.deepgram.com/reference/speech-to-text/listen-flux),
[Cartesia WebSocket TTS](https://docs.cartesia.ai/api-reference/tts/websocket),
[OpenAI Realtime WebSocket lifecycle](https://developers.openai.com/api/docs/guides/realtime-conversations#handling-audio-with-websockets),
[Gemini Live BidiGenerateContent](https://ai.google.dev/api/live),
and [Sarvam streaming STT schema](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/ws.md).
