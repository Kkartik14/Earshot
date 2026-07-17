# Pipecat adapter

Status: M1 normalization adapter.

Verified against `pipecat-ai==1.5.0`, tag commit
`f97595f9ccdec52ece7561ecf642c0eec63fdefe` on 2026-07-11. The supported package
range is `>=1.5.0,<1.6`.

Pipecat is first because its broad transport/service matrix and native OTel hierarchy
force the contract to remain framework-neutral.

## Rules

- Consume Pipecat native OTel spans plus observer facts.
- Preserve trace/span IDs, parentage, links, both resource/scope schema layers, scope
  attributes, and policy-allowed native attributes.
- Do not generate duplicate STT/LLM/TTS spans when Pipecat already emitted them.
- Keep VAD, turn commitment, STT, LLM/tool, TTS, transport, and render evidence
  semantically distinct.
- Unknown Pipecat operations survive as `framework_operation` or their explicit open
  `earshot.operation.name`.
- Missing client render is coverage, not a server-side playout guess.
- The native `turn` span is a lifecycle container that encloses sibling STT, LLM,
  and TTS spans. It is classified as `framework_operation`, never `turn_detection`.
- `turn.was_interrupted=true` accompanied by
  `turn.ended_by_conversation_end=true` is shutdown state, not user barge-in.
- `turn.was_interrupted` alone is not accepted-interruption evidence. Pipecat 1.5 can
  publish that shape while processing a normal terminal `EndFrame`, so Earshot requires
  an unambiguous interruption source before authoring `earshot.interruption.accepted`.

`PipecatAdapter.consume_span()` accepts a span-shaped mapping/object, which keeps the
normalizer testable without importing Pipecat. `create_span_processor()` and
`attach(existing_tracer_provider)` consume ended native spans without replacing the
provider or creating a second root. `create_observer()` supplies a Pipecat 1.5 frame
observer. It authors an accepted interruption only when a native `InterruptionFrame`
is observed while native `BotStartedSpeakingFrame`/`BotStoppedSpeakingFrame` state
says bot playout is active. Pipecat also broadcasts `InterruptionFrame` at the start
of an ordinary first user turn, so the interruption frame alone is not barge-in
evidence. The accepted classification is therefore explicitly `inferred` from both
native facts, cites the composite frame source, and is attached to the interrupted
native turn number tracked from Pipecat's lifecycle frames.
The observer deduplicates the same broadcast frame as it crosses processors and its
upstream/downstream sibling pair. Observer-authored events use Earshot's recorder
receipt time; Pipecat frame timestamps have a separate monotonic origin and are never
mislabeled as recorder-clock coordinates.

## Provider metrics

Pipecat reports per-stage metrics as span attributes rather than through a metrics
event bus. The adapter lifts a governed allowlist of them into a `pipeline.metric`
quality sample so the analyzer sees the same `provider_measurements` it derives from
LiveKit's metrics callbacks. The metric also stays on the operation; the sample is the
normalized view, not a replacement.

- **Allowlist, never a namespace wildcard.** Only known fields are lifted
  (`metrics.ttfb`, `metrics.character_count`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`,
  `gen_ai.usage.cache_creation.input_tokens`, and `gen_ai.usage.reasoning_tokens`). An
  additional turn-scoped field, `turn.user_bot_latency_seconds`, is retained from
  Pipecat's native turn observer. An unknown numeric attribute stays subject to the
  operation's metadata privacy rules and is never recreated verbatim as a measurement.
- **Vendor metrics are stage-scoped** (`pipecat.<stage>.ttfb`). Pipecat reports both
  LLM and TTS first-byte latency under the same `metrics.ttfb` key, and analysis keys
  provider measurements by name, so unscoped names would let one stage silently
  overwrite the other's value and evidence. Standard `gen_ai.usage.*` counters are
  LLM-only and keep their canonical names. TTFB is accepted only from native STT, LLM,
  and TTS stages; character count is accepted only from TTS.
- **Types are enforced before lifting.** Durations must be finite non-negative numbers;
  character and token counters must be non-negative integers in the portable JSON
  integer range. Invalid source values remain omitted rather than becoming measured
  evidence.
- **Provenance remains field-exact.** Each lifted source field gets its own quality
  sample whose evidence names that exact Pipecat attribute; a combined sample never
  hides which source produced a measurement.
- **Units are declared per field**, never inferred from the key, so a millisecond
  field can never be relabelled as seconds.
- **Aggregation is source-accurate.** TTFB is an instantaneous per-stage duration;
  token usage and TTS character counts are per-operation deltas, so repeated
  operations sum with all contributing evidence instead of acting like gauges.
- **User-to-bot latency is an honest server-output fallback.** Pipecat 1.5 measures
  `turn.user_bot_latency_seconds` from actual user-speech stop to its native
  `BotStartedSpeakingFrame`. Earshot exposes it as
  `pipecat.turn.user_bot_latency` and may use it for response latency when no stronger
  receive/render boundary exists. The projection is explicitly limited as
  `server_output_excludes_delivery_and_render`; it never claims client render or that
  a person heard the response.

## Golden mapping

Conformance compares normalized facts, not raw telemetry bytes. Runtime-specific
resource keys may differ, but both first adapters must agree on shared response-stage
classification, turn-correlated response work, timing-basis vocabulary, interruption
phases, coverage, privacy, and provenance.

Pipecat 1.5 assigns an integer `turn.number`; it is not expected to equal a LiveKit
speech ID. The analyzer propagates ownership through the parent graph. When native
Pipecat tracing provides no independent end-of-user-turn commitment point, derived
anchor-to-stage latency and first-token breakdowns remain `not_observed`; the lifecycle
span end is not reused as a fake anchor. The direct native user-to-bot measurement may
still populate response latency with its explicit server-output limitation.

## Limitations

- Server-only capture automatically records `client.render` as not observed.
- Transport/client render facts exist only when the relevant observer supplies them.
- Native/source attribute units are preserved; normalized Earshot durations use
  seconds, while analysis presents milliseconds.
