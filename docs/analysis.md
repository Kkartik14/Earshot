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

The current deterministic analyzer identity is `earshot.deterministic@0.2.1`.
Analyzer version is part of the storage cache key; behavior changes such as delta-window
aggregation therefore cannot reuse a projection produced by an older analyzer.

## Turn projection

Turns are a presentation projection, not graph containers. Ownership is resolved from
an explicit `turn_id` and then inherited through native OTel parentage. ChatMessage item
IDs are not treated as turns. Provider measurements without explicit or graph-derived
ownership remain under `unassigned_provider_measurements` with their real quality
sample ID.

Repeated measurements marked `delta` are summed only inside the same owned analysis
group, cite every contributing sample, and become unavailable when units or
aggregation modes conflict or the finite sum overflows. Integer counters are summed
without float coercion; a total outside the interoperable I-JSON integer domain is
unavailable instead of rounded. Instant and cumulative observations remain snapshots.
If one quality sample contains conflicting same-name snapshots, the derived scalar is
unavailable instead of selecting one by array position; the explanation still retains
each exact fact. Conflicting `render` and `client.render` coverage declarations likewise
produce one deterministic conflict limitation rather than depending on input order.
Unassigned provider samples stay separate: analysis does not manufacture session or
turn correlation merely to aggregate them.

The explanation read model keeps derived metrics separate from exact measurement
facts. A sample with an authored operation owner appears on that operation; an
operation-less sample with an authored turn owner appears in that turn's
`measurements`; only a genuinely ownerless sample appears in top-level
`unassigned_measurements`. Repeated instant/cumulative observations are never replaced
by the analyzer's selected scalar. Independent validation compares value type, value,
unit, aggregation, owner, evidence ID, limitation, confidence, and every provenance
field exposed by `ExplainedEvidence`. The immutable bundle remains authoritative for
quality-sample windows/resources and evidence attributes that the presentation schema
does not expose.

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
Failed, timed-out, cancelled, or errored operations never author synthetic endpointing,
provider-output, transport, receive, or render boundaries. A provider duration also
cannot project beyond a comparable recorded operation end. Derived latency confidence
is the weakest of clock certainty and both boundary evidence records.
When a turn anchor is absent, or preemptive generation makes a same-clock point precede
turn commitment, equivalent LiveKit/Pipecat LLM TTFT and TTS TTFB measurements feed the
same derived first-token/first-audio projections. Their native measurement names remain
unchanged for provenance, and the projection is explicitly limited as
`stage_local_excludes_turn_scheduling` rather than presented as turn-relative latency.
Native user-stop-to-bot-start measurements (`livekit.e2e_latency` and
`pipecat.turn.user_bot_latency`) outrank a derived TTS estimate, but never outrank
observed send, receive, or render evidence. Their projection carries
`server_output_excludes_delivery_and_render` so server playout cannot be mistaken for
client render or human perception.

Cross-clock subtraction requires the same explicit clock domain. Reversed comparable
time is `inconsistent`; missing/incomparable time is unavailable, never clamped to
zero. Parallel tool output reports total work plus union elapsed time separately for
each source clock/basis.

Projection arrays use a permutation-invariant presentation order: comparable points
are grouped canonically by clock domain and timestamp basis and sorted numerically only
inside that group; equal or unlocated points use their stable identity. Array position
between different groups is a serialization rule and does not assert temporal or
causal order across clocks.

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
