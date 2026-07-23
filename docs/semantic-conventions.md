# Earshot semantic profile

The authoring registry is [`semconv/earshot.yaml`](../semconv/earshot.yaml). Earshot
extends OpenTelemetry; it does not rename native framework fields or overwrite
standard `gen_ai.*`/OpenInference concepts.
The standalone registry vendors the pinned OpenTelemetry `session.id` definition and
is checked with `python scripts/check_semconv.py`; exact provenance is recorded in
[`semconv/README.md`](../semconv/README.md).

## Naming and units

- Earshot attributes use dotted OTel names with snake_case inside a segment.
- Durations use seconds in telemetry and UCUM units in metrics.
- Exact authored timestamps remain unsigned nanoseconds.
- Milliseconds are presentation units used by analysis/API output only.
- Official OTLP JSON keeps official ProtoJSON field rules; it is not rewritten to
  satisfy Earshot casing.
- Open semantic labels are producer-controlled metadata, not arbitrary source text or
  automatic proof of anonymization. Adapters retain unknown raw labels as exact
  SHA-256 labels when they are not allowlisted.

Reuse `session.id`, relevant `gen_ai.*`, `error.type`, resource attributes, and
instrumentation scope where applicable. `gen_ai.output.type="speech"` identifies a
requested modality but does not replace voice delivery/render semantics.

`earshot.language.code` is the BCP 47 language tag attached to an STT operation.
`earshot.language.probability` is a provider-reported value from zero through one and
must be absent when the provider did not perform language detection.
`earshot.stt.mode` records the governed recognition mode without retaining transcript
content.

## Operations

Classify an existing span with `earshot.operation.name`; preserve its native name.

| Standard value                         | Meaning                                             |
| -------------------------------------- | --------------------------------------------------- |
| `capture`                              | Microphone/device/application captured input media. |
| `vad`                                  | Speech/non-speech observation.                      |
| `turn_detection`                       | Endpoint/semantic decision committing a turn.       |
| `stt`                                  | Speech recognition work.                            |
| `agent` / `llm`                        | Native agent/reasoning/model work.                  |
| `tool`                                 | Tool invocation.                                    |
| `tts`                                  | Speech synthesis/generation.                        |
| `encode` / `decode`                    | Media codec processing.                             |
| `transport_send` / `transport_receive` | Application/media delivery boundary.                |
| `render`                               | Client/device output-render boundary.               |

The vocabulary is open. A producer may emit a new value without a schema revision.

VAD and turn detection must remain distinct. A native speech-to-speech provider may
expose `agent` without observable `stt` or `tts`; do not invent missing operations.
Likewise a framework `turn` lifecycle that encloses the full response is not a
`turn_detection` interval.

## Point events

Earshot-authored independent events are OTLP log records. Preserve source span events
as source evidence even when an adapter also normalizes them.

```text
earshot.capture.started / stopped
earshot.speech.started / ended
earshot.turn.proposed / committed / cancelled
earshot.transcript.partial / final
earshot.response.first_token
earshot.response.first_audio_generated
earshot.model.cancelled
earshot.audio.first_byte_sent
earshot.audio.first_packet_received
earshot.audio.queued.discarded
earshot.audio.render.scheduled / started / stopped
earshot.interruption.detected / accepted / ignored
earshot.transport.connected / reconnecting / disconnected
earshot.transport.message.duplicate / out_of_order
earshot.telephony.dtmf.received / voicemail.detected
earshot.device.route_changed / permission_denied / audio_context_suspended
earshot.fault
```

Detection, acceptance, ignored interruption, model cancellation, queued-audio
discard, and render stop are different facts. DTMF and voicemail events describe
gateway observations; they do not imply transcript or audio capture.

## Typed links

`earshot.link.type` standard values:

```text
produced_by | consumes | supersedes | retries | interrupts | handoff | duplicates
```

`earshot.link.target_scope` is `internal`, `external`, or `unknown`. An unresolved
internal target is invalid; unresolved external/unknown targets remain valid because
incident bundles may contain partial or cross-trace evidence.

## Evidence

Render, transport, perceptual, and other UX/network claims require the core fields:

```text
source, observer, method, confidence, availability
```

`source_field`, `method_version`, and an evidence `sample_window` are retained when
the source exposes them. The owning operation/event/quality record supplies its
timestamp or interval and declared clock domain; duplicating that clock into Evidence
is not required. Evidence marked unavailable/not-observed cannot accompany an asserted
render, transport, or quality value—use Coverage instead.

`measured`, `estimated`, and `inferred` are conventional confidence values, not a
closed enum. Reconstructing an interval as metric-arrival minus provider duration is
`estimated`.

## Quality

Keep quality classes separate:

- `transport`: WebRTC stats, RTCP, gateway/carrier data. Packet loss, interarrival
  jitter, and RTT belong here.
- `audio_perceptual`: clipping, silence, SNR, echo/noise, or P.563 MOS-LQO inferred
  from audio.

PCM cannot be the evidence source for network jitter/loss/RTT. P.563 is perceptual
MOS-LQO, never network/E-model MOS. Unknown measurements are represented by
availability, not a numeric zero.

V1 quality values are numeric or boolean and raw counters are numeric—never arbitrary
strings. Quality samples are incident-profile records; M1 deliberately does not define a second
`earshot.*` OpenTelemetry metric instrument.

### Raw-pipeline latency measurements

The provider-neutral `earshot.pipeline()` facade retains provider latency scalars as
quality measurements. These names describe scalars only; they do not prove operation
intervals or point-event coordinates:

| Measurement | Unit | Meaning |
| --- | --- | --- |
| `earshot.stt.ttfb` | `ms` | Provider-reported STT time to first response. |
| `earshot.stt.finalization_latency` | `ms` | Provider-reported audio-stop to final-transcript latency. A final-transcript event is authored only when speech end was independently observed. |
| `earshot.llm.ttft` | `ms` | Provider-reported model time to first token. |
| `earshot.llm.completion_latency` | `ms` | Provider-reported model completion latency. |
| `earshot.tts.ttfb` | `ms` | Provider-reported TTS time to first response. |

An explicit `earshot.response.first_audio_generated` event remains independent of
`earshot.tts.ttfb`; when a source provides both, Earshot preserves both facts.

## Output timeline

Keep these facts distinct:

1. first audio generated;
2. first byte sent;
3. first packet received;
4. render scheduled/started/stopped.

There is no `heard_at` semantic. Render evidence may itself be estimated and must say
how it was observed.
