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
adapter.attach_span_processor(existing_tracer_provider)
adapter.attach_session_listeners(agent_session)
```

The processor filters for LiveKit scope/`lk.*` spans, authors no new trace root, and
does not replace existing processors or exporters. Native interruption span events
and adaptive-interruption callbacks become correlated Earshot point events.

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
