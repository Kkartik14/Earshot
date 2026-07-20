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

## Vapi

- Accepts only `message.type=end-of-call-report` and keys the Receipt from `call.id`.
- Trust uses either `Authorization: Bearer ...` configured through a Custom Credential or
  legacy `X-Vapi-Secret`; providing both is rejected. Vapi's configurable HMAC is not a
  universal protocol, so Earshot does not invent a signature format.
- `artifact.performanceMetrics.turnLatencies` and interruption aggregates are retained as
  provider-named evidence. Official schemas do not document the latency unit, so values
  remain `provider_unit` and never feed shared millisecond projections. Association with
  an assistant message is an explicitly inferred ordered-index join; when its
  `secondsFromStart` is absent, the metrics remain session-scoped and author no turn.
- Transcript, messages, recordings, variables, customer data, and call identifiers are
  discarded or HMAC-correlated.

Primary sources: [server authentication](https://docs.vapi.ai/server-url/server-authentication),
[server events](https://docs.vapi.ai/server-url/events),
[Call artifact schema](https://docs.vapi.ai/api-reference/calls/get).

## Retell

- Accepts only `event=call_analyzed` and keys the Receipt from `event + call.call_id`.
- Trust uses `X-Retell-Signature: v={timestamp_ms},d={digest}`. The digest is HMAC-SHA256
  over `raw_body + timestamp` with the webhook API key; timestamps outside five minutes
  are rejected.
- Agent word start/end offsets author provider-aligned speech timing without retaining
  words. They do not prove transport playout, render, or that audio was heard.
- `latency.*.values` are documented milliseconds but have no stable transcript-turn join.
  They remain session-scoped provider samples and never masquerade as per-turn evidence.
- Interruption coverage is `not_exposed`; transcript, analysis, dynamic variables,
  recordings, public logs, phone numbers, and raw call/agent identifiers are discarded.

Primary sources: [secure webhook](https://docs.retellai.com/features/secure-webhook),
[webhook events](https://docs.retellai.com/features/webhook-overview),
[latency semantics](https://docs.retellai.com/reliability/check-actual-latency).

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
