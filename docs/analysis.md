# Deterministic analysis contract

M1 analysis is a replaceable, metadata-only sidecar over one exact immutable incident.
It is not an LLM explanation engine and does not mutate or redact source evidence.
The machine contract is
[`spec/derived-analysis.schema.json`](../spec/derived-analysis.schema.json).

## Binding and validation

`input_sha256` identifies the deterministic protobuf evidence artifact with embedded
analysis absent. Storage/API also bind analyzer name/version and generation time. A
sidecar is rejected when it has unknown fields, non-finite numbers, a different
session/digest, dangling evidence, a turn-ownership mismatch, an unrecognized source
clock key, an ungoverned measurement name/unit, or source-count summaries that do not
match the artifact.

Every diagnosis cites an operation, event, quality sample, or media record present in
the exact input. Turn operation/event lists and every latency/tool/interruption/provider
measurement have the same reference requirement.

## Turn projection

Turns are a presentation projection, not graph containers. Ownership is resolved from
an explicit `turn_id` and then inherited through native OTel parentage. ChatMessage item
IDs are not treated as turns. Provider measurements without explicit or graph-derived
ownership remain under `unassigned_provider_measurements` with their real quality
sample ID.

The response anchor preference is:

1. `earshot.turn.committed`;
2. `earshot.speech.ended`;
3. the end of a real `turn_detection` operation, marked as an estimate.

A whole-turn lifecycle span is never used as endpointing. This means current Pipecat
native tracing can honestly produce `turn_anchor_not_observed` while LiveKit EOU
produces a measured/estimated value.

Output facts remain separate:

1. text first token;
2. first audio generated;
3. first byte sent;
4. first packet received;
5. render started.

Provider TTFT/TTFB can project a point only when its semantics are known. LiveKit
RealtimeModelMetrics TTFT is first audio token, so it authors first-audio-generated and
never a text-token fact. The response metric uses the strongest available boundary and
labels a fallback `receive_estimate`, `transport_estimate`, or `tts_estimate`.

Cross-clock subtraction requires the same explicit clock domain. Reversed comparable
time is `inconsistent`; missing/incomparable time is unavailable, never clamped to
zero. Parallel tool output reports total work plus union elapsed time separately for
each source clock/basis.

## Current diagnosis boundary

The M1 analyzer emits a measured `operation.failed` diagnosis only for an operation
whose governed status is `error`, `timeout`, or `failed`. Missing render, unsupported
stages, and incomplete clocks are limitations, not invented causes. The deterministic
fault corpus validates representability plus scenario-specific ordering, bottleneck,
causality, provenance, and absence assertions across all thirteen scenarios. It does
not claim every scenario is automatically root-caused today.

Broader incident explanation, counterfactual reasoning, and incident-to-regression
fixture conversion are later milestones. They should consume this evidence-linked
sidecar contract rather than adding prose or payloads to the immutable bundle.
