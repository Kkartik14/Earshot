# LiveKit adapter

Status: M1 normalization adapter; client render is M1.5.

Verified against `livekit-agents==1.6.5`, tag commit
`dbc38dfbc113a2e06d36feb1278e543af3585ea9` on 2026-07-11. The supported package
range is `>=1.6.5,<1.7`.

## Rules

- Attach without replacing the customer's tracer provider or exporters.
- Consume LiveKit native OTel plus supported metrics callbacks.
- Preserve native trace identity and policy-allowed `lk.*` source facts.
- Treat `VADMetrics` and `EOUMetrics`/turn commitment as different evidence.
- Correlate by native speech/generation identity when available.
- Reconstructed `metric arrival - provider duration` timing is `estimated`, not raw.
- Server TTS/audio duration is not client render proof.
- Treat `user_turn`/generic `turn` spans as lifecycle containers; only
  `eou_detection`/EOU metrics are endpoint-detector evidence.

With a metrics-only listener, the normalizer maps callback objects to these facts:

| LiveKit metric         | Earshot fact                       |
| ---------------------- | ---------------------------------- |
| `VADMetrics`           | `pipeline.metric` quality sample   |
| `EOUMetrics`           | `turn_detection` operation         |
| `STTMetrics`           | `stt` operation                    |
| `LLMMetrics`           | `llm` operation                    |
| `RealtimeModelMetrics` | `agent` operation                  |
| `TTSMetrics`           | `tts` operation                    |
| `InterruptionMetrics`  | `interruption_detection` operation |
| `EOTInferenceMetrics`  | `turn_detection` operation         |
| `AvatarMetrics`        | `avatar` operation                 |

`VADMetrics` is an aggregate callback window, not one discrete invocation. Its
`inference_duration_total` and `inference_count` reset after each LiveKit emission,
so Earshot records them as `delta` measurements. `idle_time` is an `instant`
measurement. Repeated turn-correlated delta windows are summed by analysis and cite
every contributing quality-sample ID; instant values are never summed.
Per-request usage, audio, session-duration, character, interruption, and EOT request
counts are also deltas. Repeated requests therefore sum with every request's
evidence; TTFT/TTFB and detector latency values remain instantaneous measurements.
LLM `prompt_tokens`/`completion_tokens` and realtime-model `input_tokens`/
`output_tokens` use the canonical `gen_ai.usage.*` namespace. STT and TTS token
counts remain stage-specific under `livekit.stt.*` and `livekit.tts.*`; their audio
durations are likewise separate, and realtime `session_duration` is retained as
`livekit.realtime.session_duration`. Earshot never combines STT input usage with TTS
output usage merely because LiveKit uses the same source field names.
Realtime `input_token_details`/`output_token_details` retain audio, text, and image
counts under `gen_ai.usage.*`. The nested cached-input total and cached modality
breakdown are retained too. These per-response counters are deltas and accept only
I-JSON integers; invalid nested values are omitted independently rather than
discarding the rest of the metric.

STT, TTS, and realtime `acquire_time` map to stage-specific
`livekit.<stage>.connection_acquire_time` instant measurements. LiveKit 1.6 reports
standalone STT/realtime connection acquisition using the ordinary metric class with
an empty `request_id` and zero usage. Earshot retains that callback as a quality
point only: it does not create a request operation, zero-valued usage deltas, or a
false `no_audio_token` coverage fact. A zero `acquire_time` sentinel is omitted while
the measured `connection_reused` decision remains available. This connection-only
callback remains owned by the metrics listener even when native spans are enabled
because it has no owning request span.

Provider counters are accepted only as non-negative I-JSON integers (maximum
`9007199254740991`). Booleans, fractional values, negatives, and larger integers are
omitted from operation attributes and quality samples. An invalid
`num_interruptions` value also cannot author an interruption event.
LiveKit Agents 1.6 `VADMetrics` does not expose a request, speech, or turn ID, so
real callback windows normally remain session-level
`unassigned_provider_measurements`. Earshot preserves every raw delta window but
does not invent turn ownership or silently combine unassigned callbacks. A runtime
that supplies explicit correlation can use the turn-level delta aggregation.

The quality-sample point uses the provider timestamp when it is present, then the
explicit `observed_at` supplied by the caller, and only then the recorder clock.
Window identity includes correlation IDs, the explicit time, and safe framework,
metric-label, model, and provider dimensions. This makes an identical callback
idempotent while preserving successive windows. Exact timestamp-less callbacks with
no `observed_at` collapse by content because no stronger window identity exists.
Unsafe metric labels are hashed by the metadata policy.

`LiveKitAdapter.consume_metric()` is duck typed so mapping and privacy behavior can
be tested without installing LiveKit. It returns the retained fact ID, or `None` when
a VAD callback contains no numeric measurements and therefore authors no quality
sample. `attach_metrics_listener()` registers a fail-open metrics callback and never
installs a new tracer provider.

The real-package integration lane constructs the required 1.6.5 metric fields
(`label`, request IDs, cancellation/token/audio/streaming fields) rather than relying
only on permissive fixture dictionaries. `EOUMetrics.on_user_turn_completed_delay`
maps to `earshot.duration.turn_callback_seconds`; documented zero sentinels remain
missing duration, never measured zero.

`consume_span()` accepts ended OpenTelemetry `ReadableSpan`-shaped values and
preserves trace/span/parent IDs, links, resource attributes/schema URL,
instrumentation scope name/version/attributes/schema URL, timestamps, status, and
policy-allowed attributes. Span-derived point events and provider-quality samples
carry the same provenance. It rejects conflicting duplicates and treats an identical
callback as idempotent.

```python
adapter = LiveKitAdapter(recorder, framework_version="1.6.5")
handle = adapter.attach_span_processor(existing_tracer_provider)
adapter.attach_session_listeners(agent_session)

try:
    # Keep this scope active while starting the session and spawning its tasks.
    with handle.session_scope():
        await agent_session.start(...)
        await run_voice_session(agent_session)
finally:
    existing_tracer_provider.force_flush()
    handle.close()
```

The shared processor filters for LiveKit scope/`lk.*` spans, authors no new trace
root, and does not replace existing processors or exporters. `session_scope()` uses
an opaque registration key, not the caller's `session_id`, so two active incidents
may safely have the same external room/session name. Always use the scope: a lone
session can be routed unambiguously without it, but an unscoped span is quarantined
as soon as another session shares the provider. Close the handle only after the
framework has ended its spans and the provider has flushed.

Quarantined spans are never copied into another incident. Each active handle exposes
content-free health through `handle.status.quarantined_span_count`, and each affected
incident records `livekit.span.routing=partial` with reason
`unattributed_span_quarantined`. Span attributes are not included in that diagnostic.
Native interruption span events and adaptive-interruption callbacks become correlated
Earshot point events.

`create_span_processor()` is retained only as an explicit migration error because a
recorder-bound processor installed per session bypasses isolation. Use
`attach_span_processor(existing_tracer_provider)` instead.

When both documented surfaces are attached, ownership is type-selective. Native spans
own LLM, TTS, and realtime operations plus their embedded metric samples. EOU/EOT
callbacks add supplementary quality and commitment evidence. Because LiveKit 1.6 does
not guarantee equivalent native operation spans for the remaining metric types, STT,
interruption, and avatar callbacks create operations, while callback VAD aggregate
windows create quality samples. An ended native `vad` span is still a real `vad`
operation. This prevents duplicate LLM/TTS/realtime work without erasing metric-only
stages. Nested node/request/attempt spans keep their native name in
`earshot.framework.operation.name`; literal operation counts are not used as
cross-runtime equivalence. Session listeners use current 1.6.5 event types
`overlapping_speech` and `agent_false_interruption`.

LiveKit 1.6 creates each `InterruptionMetrics` callback from an
`OverlappingSpeechEvent`, but gives the two surfaces no shared request/speech ID and
stamps the metric later with a separate wall-clock read. When overlap listeners are
attached, `overlapping_speech` therefore exclusively owns interruption point facts;
`InterruptionMetrics` still owns its detector operation and aggregate delta quality.
Earshot does not use a lossy timestamp window or duration fingerprint, so two nearby
interruptions with identical detector values remain two events. Interruption
durations must be finite and non-negative, probability must be within `[0, 1]`, and
request counts must be non-negative I-JSON integers. Invalid fields are omitted.

Current `ChatMessage.metrics` is retained without message content. A ChatMessage item
ID is not a turn ID; only explicit `turn_id`/`speech_id` correlates it. An interrupted
item records the accepted decision as a standalone/listener-only fallback when its
metrics are absent. In dual-surface mode that unkeyed fallback is suppressed because
the correlated native `agent_turn` span is authoritative.

RealtimeModelMetrics TTFT means first **audio** token. Parsed
`lk.realtime_model_metrics` authors `earshot.response.first_audio_generated`, never a
text-token event, and correlates through the native parent graph. Opaque
`lk.participant_id` and participant kind become typed ownership; participant identity
remains opt-in identity payload.

## Render limitation

Until a browser/mobile collector provides receiver/render evidence, the adapter emits:

```text
signal: client.render
availability: not_observed
reason: server_cannot_observe_client_render
```

No `playout.confirmed`, `heard_at`, or user-audible response claim may be synthesized.
