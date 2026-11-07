# Hosted-provider Connectors

A Connector binds one Project to one provider delivery surface. Its endpoint identifier
is opaque, and its secret configuration is an environment reference such as
`env:ELEVENLABS_WEBHOOK_SECRET`; secret values never enter SQLite.

## Processing contract

Every delivery crosses one ordered boundary:

```text
bounded raw bytes
  -> provider authentication over the exact body/header contract
  -> authenticated per-Connector rate limit
  -> strict JSON (UTF-8, no duplicate keys/non-finite values, bounded depth)
  -> provider normalization with metadata-only capture
  -> durable Delivery Receipt claim
  -> canonical Incident + Turn Fact publication
  -> HMAC-only External Identity correlation
  -> Receipt completion, then 2xx acknowledgement
```

The Receipt key is an instance-keyed HMAC of the provider's stable event identity. The
normalizer's session/bundle fingerprints and External Identities are also scoped to the
stored Connector, so the same provider call delivered to two Connectors cannot cross
Project boundaries. Raw identities and raw bodies are not persisted. An exact retry
replays the prior result; the same delivery identity with changed bytes is `409`; an
active processing lease is a retryable `503`. Receipt completion is bound to the current
lease attempt, so a stale worker cannot complete a reclaimed delivery. Authentication
failures occur before parsing or storage. An unresolved configured secret is a bounded,
retryable `503` configuration failure rather than a provider-authentication `401`. For
zero-downtime provider-secret rotation, set both `NAME` and `NAME_PREVIOUS` while providers
transition, then remove the previous value.

## ElevenLabs Agents

- `post_call_transcription`: retains turn timing and
  `conversation_turn_metrics.convai_llm_service_ttfb.elapsed_time`; transcript messages,
  analysis, dynamic variables, conversation/agent IDs, and URLs are discarded. The
  documented provider status enum is allowlisted and mapped to controlled Earshot session
  states; unknown or free-form status values are rejected rather than retained.
- `post_call_transcription_otel`: preserves OTLP trace/span/parent identity and source
  timestamps from the finalized `resourceSpans` batch. Text and tool-result attributes
  are discarded. Span duration is not re-labelled as model latency. Trace/span IDs are
  classified as governed canonical graph identity for this OTLP-shaped format; they are
  not returned as External Identities or logged separately.
- `post_call_audio`: authenticates and records an ignored Receipt; audio is not ingested.
- Trust: `ElevenLabs-Signature: t=...,v0=...`, HMAC-SHA256 over `timestamp.raw_body`, with
  a five-minute skew limit and rotation-aware constant-time comparison.

Primary sources: [post-call webhooks](https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks),
[OpenTelemetry traces](https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces).

## Vapi (not production-supported)

This adapter has synthetic conformance coverage only; no real signed delivery has been
verified. The notes below describe current parser behavior, not verified delivery support.

- Accepts only `message.type=end-of-call-report` and keys the Receipt from `call.id`.
- Trust uses either `Authorization: Bearer ...` configured through a Custom Credential or
  legacy `X-Vapi-Secret`; providing both is rejected. Vapi's configurable HMAC is not a
  universal protocol, so Earshot does not invent a signature format.
- `artifact.performanceMetrics.turnLatencies` can produce indexed metric spans, but the
  adapter does not match them to transcript messages by message ID or time offset. Any
  ordinal association is inferred, and emitted latency units are unverified against a real
  delivery. The adapter does not emit Vapi's interruption aggregate counters.
- `artifact.messages` can produce transcript-line evidence under capture presets that
  allow transcript text; metadata-only capture removes that text and the normalized
  `gen_ai.agent.id`. Call identity is HMAC-correlated, and unselected request fields are
  not retained.

Primary sources: [server authentication](https://docs.vapi.ai/server-url/server-authentication),
[server events](https://docs.vapi.ai/server-url/events),
[Call artifact schema](https://docs.vapi.ai/api-reference/calls/get).

## Retell (not production-supported)

This adapter has synthetic conformance coverage only; no real signed delivery has been
verified. The notes below describe current parser behavior, not verified delivery support.

- Accepts both `call_ended` and `call_analyzed`, keyed by `event + call.call_id`. Both
  include call, transcript and recording facts; only `call_analyzed` adds analysis, cost
  and latency facts, and either event marks the Call ended.
- Trust uses `X-Retell-Signature: v={timestamp_ms},d={digest}`. The digest is HMAC-SHA256
  over `raw_body + timestamp` with the webhook API key; timestamps outside five minutes
  are rejected.
- Agent word start/end offsets time normalized transcript lines; raw word arrays are not
  retained. They do not prove transport playout, render, or that audio was heard.
- `latency.*.values` are documented milliseconds. The `e2e` values also create indexed
  turn-latency facts by array order, without joining them to transcript messages by ID or
  time; that ordinal association is not verified against a real delivery.
- Interruption coverage is `not_exposed`. The normalizer selects transcript, analysis,
  cost, latency, and recording evidence; the active Capture Preset governs transcript and
  media storage. Call IDs are HMAC-correlated, and `gen_ai.agent.id` is removed by
  `metadata_only` capture. Unselected request fields are not retained.

Primary sources: [secure webhook](https://docs.retellai.com/features/secure-webhook),
[webhook events](https://docs.retellai.com/features/webhook-overview),
[latency semantics](https://docs.retellai.com/reliability/check-actual-latency).

## Ringg

- Accepts the consolidated terminal `all_processing_completed` delivery; progress
  deliveries authenticate and produce an ignored Receipt.
- Trust uses exactly one configured static subscription credential, either
  `Authorization: Bearer ...` or `X-Webhook-Secret`. Ambiguous dual headers are rejected.
- Provider call IDs are retained only as instance-keyed HMAC correlations. Transcript,
  phone numbers, recordings, analysis, tool logs, agent/workspace IDs, and custom values
  are discarded.
- Call duration, overall latency, and first utterance are retained as provider session
  aggregates in seconds. They do not author per-turn timing or client-render claims.

Primary source: [webhook request and response payloads](https://docs.ringg.ai/webhooks/payloads).

## Provisioning

```bash
earshot project create support --display-name "Support Voice" --data-dir /data
earshot api-key issue --project support --label production --data-dir /data
earshot connector create --project support --provider elevenlabs \
  --secret-env ELEVENLABS_WEBHOOK_SECRET --data-dir /data
```

The last command prints the provider hook path. Configure that HTTPS URL at the provider
and inject the named secret environment variable into the Earshot process.

## Deliberate limits

Connectors consume finalized provider records. They do not make a generic live OTLP
receiver safe: generic OTLP still needs explicit session identity, completion, late-span
revision, crash recovery, multi-trace correlation, and privacy policy. Audio custody and
provider REST backfill are also separate workflows rather than implicit webhook behavior.
