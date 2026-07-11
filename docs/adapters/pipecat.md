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

`PipecatAdapter.consume_span()` accepts a span-shaped mapping/object, which keeps the
normalizer testable without importing Pipecat. `create_span_processor()` and
`attach(existing_tracer_provider)` consume ended native spans without replacing the
provider or creating a second root.

## Golden mapping

Conformance compares normalized facts, not raw telemetry bytes. Runtime-specific
resource keys may differ, but both first adapters must agree on shared response-stage
classification, turn-correlated response work, timing-basis vocabulary, interruption
phases, coverage, privacy, and provenance.

Pipecat 1.5 assigns an integer `turn.number`; it is not expected to equal a LiveKit
speech ID. The analyzer propagates ownership through the parent graph. When native
Pipecat tracing provides no independent end-of-user-turn commitment point, response
latency remains `not_observed`; the lifecycle span end is not reused as a fake anchor.

## Limitations

- Server-only capture automatically records `client.render` as not observed.
- Transport/client render facts exist only when the relevant observer supplies them.
- Native/source attribute units are preserved; normalized Earshot durations use
  seconds, while analysis presents milliseconds.
