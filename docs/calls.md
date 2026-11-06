# Calls: the call view, the calls list, cost and search

Since 0.3.0, every closed Call gets a **call view**: its audio with a waveform, the
transcript on the audio's time axis, per-turn latency as the framework reports it beside
the **audible gap at the agent** measured from the recording, signals across the call,
and its cost. The calls list filters on all of these and searches transcript text.

The bundled viewer shows them at `/calls` and `/calls/<call_id>`. This page describes
what the server computes and the API the viewer reads; the
[OTLP quick start](./otlp-quickstart.md) shows how to send a call. The exact API schemas are in
[`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json).

## What a call needs

- **Spans** from Pipecat 1.6 or LiveKit Agents 1.6.5–1.8.x, or in the
  [host span profile](#the-host-span-profile) from a host that runs its own pipeline,
  keyed by a Session Key. They give the turns, the reported latency, the transcript and
  the usage.
- **A Recording on one timeline**, for the audible gap and the signals: one stereo
  Recording with `channel_speakers: ["user", "agent"]` (WAV, FLAC or Ogg Opus), or a
  `user` and an `agent` mono Recording with the same `started_at_unix_nano`,
  durations within 20 ms, and no other mono user or agent Recording at that start.
  Without one, no turn is measured, and the reason says why.
- **The `signals` extra** on the server (`pip install 'earshot-observability[server,signals]'`;
  the container image includes it). Without it every analysis is `unavailable` with
  `EARSHOT_SIGNALS_EXTRA_REQUIRED`; installing it requeues them at the next start.

Optionally, label calls with three span attributes (on any span of the call):

| Attribute              | Becomes                                                                                                                                        |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `earshot.agent.name`   | `agent`. Else `gen_ai.agent.name`, else LiveKit's `lk.agent_name`, else the resource `service.name` (`unknown_service…` counts as absent).     |
| `earshot.call.outcome` | `outcome`, any string. Else `error` if a span has error status, else `interrupted` if the call closed without an end signal, else `completed`. |
| `earshot.call.tags`    | `tags`: the union of every span's string array (or single string).                                                                             |

## How a call gets its view

```text
Recording stored ──▶ analysis (a child process per Recording) ──┐
Revision built   ───────────────────────────────────────────────┼──▶ projection ──▶ call view, list columns
price added      ───────────────────────────────────────────────┘
```

1. **Analysis.** Each stored Recording is analysed once, by the server, in a child
   process: speech runs per channel with each edge's uncertainty, the noise floor, and
   waveform peaks. A mono user/agent pair on one timeline also gets one stereo FLAC
   rendition, so it plays as one file. See [production notes](./production.md#recording-analysis-and-the-signals-extra).
2. **Projection.** After each Revision, each analysis result and each price change, the
   server recomputes the Call's projection from its stored spans, its latest Revision's
   Recordings and analyses, and the prices.

A call detail and every list item carry `projection_state`:

| `projection_state` | Meaning                                                                                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `none`             | No projection yet (`projection: null`). An open Call has none. The viewer shows the span transcript instead.                                              |
| `stale`            | The Call, its Project's prices, the built-in prices or the projection algorithm changed since it was computed. It is still served until it is recomputed. |
| `current`          | Up to date.                                                                                                                                               |

Until an older projection is recomputed, its reported-latency summary is `null` if its
projection version predates the current latency semantics. Nonpositive per-turn values
are `null` as well, and latency filters never match unknown values.

`projection.analysis_state` summarizes the Revision's Recordings: `none` (no Recording),
`pending` (an analysis is still running), `stored` (all stored), `partial` (all finished,
some stored), `failed` or `unavailable`. To wait for a finished view, poll
`GET /v1/calls/<call_id>` until `projection_state` is `current` and `analysis_state` is
not `pending`. For a 30-second call this is about half a second after the Call closes.

**What a projection reads.** Exactly the evidence its Revision holds: a span or Recording
stored after the Revision was built changes the projection only once a later Revision
holds it. A Recording's duration is the one its analysis decoded, else its header's.
Only WAV, FLAC and Ogg Opus Recordings are split into one media channel per speaker;
MP3 and WebM, whose headers Earshot does not read, are one media reference under the
Recording's own speaker.

When a catalog is upgraded to this feature, the Revisions it already holds keep the
evidence they were built from: each stored span and Recording counts in its Call's latest
Revision, except evidence stored after that Revision was built, which the next Revision
picks up. Evidence stored while the latest Revision was being built cannot be told from
what it holds, and counts in it.

**Out-of-range values are unknown, never clamped.** A time outside the catalog's signed
64-bit nanosecond range, and a reported latency or other producer-reported duration whose
nanoseconds do not fit it, are unknown (`null`). So is a cost whose total does not
fit 64 bits, which also makes the cost incomplete. A label (agent, outcome or tag)
holding U+0000 is absent. U+0000 is removed from the normalized transcript used by the
call view and search. With full capture, the original transcript remains in the stored
source span and full-capture Revision; the normalized Call transcript removes U+0000.
A Revision holds it only while it fits the recorder's
64 KiB per-value and aggregate capture budgets. A host-profile transcript over 64 KiB
is invalid: the offline checker reports it, and Earshot does not index it as a Call
transcript or copy it into a Revision. The original source span remains available under
`full`, subject to recorder capture budgets. `metadata_only` removes transcript content
before storage.

**One bad Call does not stop the others.** A Call whose projection cannot be stored is
logged with `EARSHOT_PROJECTION_FAILED` and one of these codes, then set aside and retried
after 1 minute, then 4, then every 16, or as soon as it changes. The server keeps
projecting other Calls.

| Code                               | Meaning                                                                    |
| ---------------------------------- | -------------------------------------------------------------------------- |
| `EARSHOT_PROJECTION_INPUT_INVALID` | The Call's stored inputs could not be read or projected.                   |
| `EARSHOT_PROJECTION_OUT_OF_RANGE`  | A value of the projection does not fit its column, or a text holds U+0000. |
| `EARSHOT_PROJECTION_WRITE_REFUSED` | The database refused the write.                                            |

## The call view: `GET /v1/calls/<call_id>`

Every 0.2.0 field is kept. `projection` adds:

| Member                                          | What it holds                                                                                                                                                                                                                                             |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent`, `framework`, `outcome`, `tags`         | As above.                                                                                                                                                                                                                                                 |
| `duration_ms`, `turn_count`, `measurable_turns` | The call's length and turns; `measurable_turns` counts the turns with a measured gap.                                                                                                                                                                     |
| `reported_latency_p50_ms`, `_max_ms`            | Over the turns whose framework reported a latency.                                                                                                                                                                                                        |
| `audible_gap_p50_ms`, `_max_ms`                 | Over the measured turns only; null when none was measured.                                                                                                                                                                                                |
| `cost`                                          | `lower_bound_nano_usd`, `estimated_nano_usd`, `unknown_items`, `complete`. See [cost](#cost).                                                                                                                                                             |
| `axis_origin_unix_nano`, `axis_origin`          | Time zero of every `*_ms` below: the earliest Recording's start (`recording_start`), or the first evidence when there is none.                                                                                                                            |
| `media[]`                                       | What to play: `content_path` (Range), `peaks_path`, `content_type`, `offset_ms`, `duration_ms`, `channels[]` (one lane each, `user` or `agent`), and its `analysis_state` and `analysis_error_code`. A rendition replaces the mono pair it was made from. |
| `turns[]`                                       | One per framework turn: its stable parent `turn_span` reference when available, optional framework `turn_id`, window, `reported_latency_ms`, `audible_gap`, and `spans[]` for detail drawers.                                                             |
| `signals`                                       | See [signals](#signals).                                                                                                                                                                                                                                  |
| `cost_items[]`                                  | Each priced, estimated or unknown item.                                                                                                                                                                                                                   |
| `transcript[]`                                  | The placed transcript. See [transcript placement](#transcript-placement).                                                                                                                                                                                 |

Unknown is `null`, never `0`. The response is `Cache-Control: no-store` with a strong
`ETag`; `If-None-Match` with it answers 304.

## Live turns for an open Call: `GET /v1/calls/<call_id>/live-turns`

The target is API 0.17.0 in the 0.5.0 line. This additive, read-only route requires
`earshot:calls:read` in hosted mode; the existing detail route and its response stay
unchanged. A 200 response contains `call_id`, `state` (`open` or `closed`), `revision`
(null while open or when a closed Call has no Revision; otherwise the latest Revision
number) and `turns[]`. Each turn has the same fields and meaning as
`projection.turns[]` above. The response also carries `axis_origin_unix_nano` and
`axis_origin`, which define the origin for its `*_ms` values. Both are null for a closed
Call without a Revision, whose response has no projection. While a Call is open, newly
stored Recordings can change which evidence defines the origin, and later evidence may
have an earlier timestamp. Times may therefore re-base between responses; render each
response against its own origin. `state` describes the Call; there is no separate
per-turn `finished` flag.

While the Call is open, the route runs the existing Call projector over the Call's
accepted, stored spans after the project's capture-preset filtering, plus any stored
Recordings and their available analyses. Those analyses can contribute audible-gap
fields; the route returns no recording media. It returns a turn only when its parent
turn span has arrived: `turn` for Pipecat and the host profile, or
`agent_turn` for LiveKit. It does not infer turns from child spans. The parent span is
final when exported; later accepted children may fill in more projected fields. Until
those children arrive, unobserved values keep the projector's existing null or
`not_measurable` meanings. The route does not persist a provisional projection or create
a Revision.

After the Call closes with a Revision, the route uses the same projector over only the
evidence held by the latest durable Revision; it does not include evidence that the
Revision did not capture. A Call that closes without a Revision has `revision: null` and
no turns. Late evidence for such a Call stays out of this response until a new Revision
captures it. Once the durable projection is current, the route's `turns[]` match it field
for field. A turn's `spans[]` contains only the existing projected summaries (IDs, name,
role, status and times); it never includes
raw span attributes or events. The route never includes audio, transcript or prices.
Missing and purged Calls answer the same 404 `EARSHOT_CALL_NOT_FOUND` as the detail
route.

Responses are `Cache-Control: no-store` and carry a strong `ETag`; matching
`If-None-Match` answers 304. The validator is tied to the same input snapshot as the
response and changes when a new span is stored for the Call, including a later child
that enriches an already returned turn, even if that child does not change the current
JSON fields. A stored Recording analysis, Call state or Revision change, or capture-preset
change also changes the validator. Hosts can poll every two seconds. The ETag rarely
matches while a Call is live and spans continue to arrive; it is most useful when the
input snapshot is quiet.
The target is at most 1 s p95 from accepted span to route visibility and at most 5 s p95
from runtime turn end to the host UI at that polling interval.

The bundled viewer polls this route every two seconds while the Call is open or its detail
projection is not `current`. With no projection, it shows live turns and labels the turn
times and span transcript from the live response's exact axis origin. It announces when
that exact origin rebases, even if the number of turns stays the same. The open Call's
span-transcript text is a snapshot from the last Call-detail read; reload the view to see
new transcript lines before close. If a projection exists while the Call is open or its
detail projection is absent, stale or on a different
revision or axis, the viewer shows live turns without playback controls, hides that
projection's media and transcript until the matching detail projection is ready. It
restores playback with the matching detail projection and stops recurring live polling
once `projection_state` is `current`. A current projection
whose `analysis_state` is `pending` keeps the detail query polling: when analysis finishes
it can invalidate the projection, after which the viewer resumes live-turn polling until
the replacement projection is current. A closed Call without a Revision remains not current
until a later Revision captures its evidence, so the viewer continues checking for that
change.

The live-turn and detail queries retry network failures, 408, 429 and 5xx responses up to
three times after the initial request. While a closed Call's projection is absent, stale or
awaiting analysis, a transient detail-refresh failure leaves the last loaded view visible
and the viewer retries every two seconds. Other 4xx responses stop retries and recurring
polling.

**Local capacity measurement (2026-11-07).** On SQLite, each Call held 300 or 1,000
synthetic turns. For 12 seconds, an emitter accepted one additional unprojected child span
per second while 16 or 32 viewers polled every two seconds, with poll start times staggered
evenly across each two-second interval. Route latency is the HTTP request through reading
the full 200 body; emitter latency includes in-process synthetic span/request construction,
conversion and `CallStore.record_spans` acceptance, but excludes the OTLP/HTTP ingest path.

| Turns | Viewers | Route 200 p95 | Route 200 max | Emitter ack p95 | Emitter ack max |
| ----: | ------: | ------------: | ------------: | --------------: | --------------: |
|   300 |      16 |       43.5 ms |       65.4 ms |          8.5 ms |          8.6 ms |
|   300 |      32 |       37.8 ms |       71.6 ms |          8.6 ms |          8.7 ms |
| 1,000 |      16 |      143.8 ms |      159.5 ms |         13.5 ms |         14.0 ms |
| 1,000 |      32 |      193.5 ms |      299.1 ms |         59.8 ms |         60.6 ms |

The live-turn bodies were 249,257 bytes (243.4 KiB) at 300 turns and 833,058 bytes
(813.5 KiB) at 1,000. With a stable input snapshot, cached 200 responses had a 0.8–1.3 ms
median and conditional 304 responses had a 0.7–1.1 ms median. Nearly all concurrent polls
returned 200 because the emitter changed the ETag between polls. In the 1,000-turn, 32-viewer
case, the 191 full bodies over 12 seconds represent about 106 Mbps of response-body egress
per continuously changing Call, before HTTP overhead. This short local SQLite run describes
the measured workstation envelope; it does not model a hosted deployment or replace the
accepted-span visibility target above.

### The audible gap at the agent

For each turn, the gap from the end of the user's speech to the start of the agent's
speech, both read from **one recorder timeline**, with ± the combined uncertainty of the
two speech edges (√(σu² + σa²)). It is measured at the agent's recorder: it is not what a
caller heard over a network.

| `status`         | `value_ms` | `bound_ms`                            | Viewer                                              |
| ---------------- | ---------- | ------------------------------------- | --------------------------------------------------- |
| `measured`       | the gap    | null                                  | `1.51 s ± 0.08`                                     |
| `not_measurable` | null       | the raw gap, for a discontinuity only | `≤ 3.08 s`, else `—`, with the reason               |
| `talk_over`      | null       | null                                  | "talk-over": the agent started while the user spoke |

| `reason`                          | Meaning                                                                                                                                                                                                                                                           |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recorder_timeline_discontinuity` | The recorder inserted time inside the gap (Pipecat's recorder only ever inserts time), so the raw gap is only an upper bound; or a host declared its recording timeline not continuous across the turn (`earshot.turn.timeline_continuous=false`), with no bound. |
| `no_agent_audio`                  | No agent speech near the framework's agent-on point.                                                                                                                                                                                                              |
| `no_clear_pairing`                | No user speech ends before the agent's, or other agent speech lies between.                                                                                                                                                                                       |
| `not_one_timeline`                | The Recordings are not on one timeline (different starts, durations over 20 ms apart, or an ambiguous mono pair).                                                                                                                                                 |
| `no_recording`                    | The Revision has no Recording.                                                                                                                                                                                                                                    |
| `analysis_not_stored`             | The analysis is pending, failed or unavailable; `analysis_state` says which.                                                                                                                                                                                      |
| `boundary_uncertainty_too_high`   | The combined speech-edge uncertainty exceeds 100 ms, so the gap is withheld.                                                                                                                                                                                      |

`basis` is `anchors_checked` (Pipecat: the turn's span anchors were checked against the
audio for a discontinuity) or `continuous_timeline_assumed` (LiveKit, whose recorder
places frames by wall clock, and the host span profile). `user_end_ms` and `agent_start_ms` are the paired speech
edges on the axis.

The speech edges come from Silero VAD, refined to 10 ms energy frames; each edge's σ is
the distance between the two (at least 10 ms). A gap is measured only when the combined
uncertainty is at most 100 ms. A clean TTS reply typically gives ± 40 to 90 ms. The
accuracy of the gap on natural human speech has not been measured against labelled audio
yet.

### Signals

Each is computed from the speech runs of the one timeline. When there is no timeline,
every cross-channel value (overlap, barge-ins, talk-overs, silences, dead air) is null
and `one_timeline` is false or null.

| Signal               | Definition                                                                                                                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `talk_time_*_ms`     | The sum of the channel's speech runs.                                                                                                                                                        |
| `overlap_ms`         | Time both speak at once, counting each interval of at least 120 ms.                                                                                                                          |
| `barge_in_count`     | The user starts speaking inside an agent run, after its first 100 ms, and is not a backchannel (shorter than 600 ms with the agent still speaking 1.5 s later).                              |
| `talk_over_count`    | The agent starts speaking inside a user run, after its first 100 ms.                                                                                                                         |
| `long_silence_count` | Silences of 2 s or more on both channels, leading and trailing silence excluded.                                                                                                             |
| `dead_air_ms`        | Silence the agent owns: a response gap of 2 s or more after the user, a gap of 3 s or more between two agent runs with no user speech, or 2 s or more before an agent-first call's greeting. |

The thresholds are named constants in `earshot.call_timeline`.

### Transcript placement

The transcript is the span transcript (Pipecat: each `stt` span's text and each `llm`
span's output; LiveKit: each `user_turn`'s transcript and each `agent_turn`'s played text,
never a discarded preemptive reply), placed on the call's axis. A user line snaps to the
start of the nearest user speech within 500 ms; an agent line to its turn's paired agent
speech. A line in a turn with a discontinuity is `alignment_uncertain`.

Each line has a `timing`: `measured` (start and end), `start_only`, or `order_only`
(listed in order, never placed, never seekable). An end time is never inferred from the
next line's start.

The call's raw `transcript` field carries the same `timing` value. Its Unix-nanosecond
times are both present for `measured`, start only for `start_only`, and both null for
`order_only` lines.

### Cost

| Item `status`  | When                                                                                                            |
| -------------- | --------------------------------------------------------------------------------------------------------------- |
| `priced`       | The usage was reported, and a price matches.                                                                    |
| `no_price`     | The usage was reported, and no price matches.                                                                   |
| `not_reported` | A request left its usage unknown (one item per request).                                                        |
| `estimated`    | STT seconds estimated from user audio duration when no request for that provider/model reports metered seconds. |

When an exact quantity cannot round-trip through a finite JSON number, `quantity` is
`null`, never clamped or rounded to a different value. The projection retains its exact
integer-milli quantity for repricing. Host-reported STT seconds are rounded to
milliseconds per request, with at least one millisecond for any positive report; cost
itself is rounded half up to a whole nano-USD.

`lower_bound_nano_usd` sums the priced items; `estimated_nano_usd` sums the estimated
ones; `unknown_items` counts the rest. A cost is `complete` when it has a lower bound and
no unknown item. The viewer shows `$0.0090` for a complete cost, else
`≥ $0.0046 · 2 unknown`, and the estimate apart as `+ ~$0.0042 (estimated)`. Money is
integer nano-USD (10⁻⁹ USD). The rules below describe which spans are billed and why.

## The host span profile

For a host that runs its own pipeline rather than Pipecat or LiveKit, and exports its
calls' turns itself. Every span is under the instrumentation scope `earshot.host`, with
times on the host's wall clock: the clock its Recordings' `started_at_unix_nano` is on.
The profile's version is `host_profile_version` in [`GET /version`](./backend-api.md#get-version).
The current implementation reports 0.2.0 and continues to accept 0.1.0 spans. Profile
0.2.0 adds optional provider-metered STT and TTS usage attributes and optional transcript
text on speech spans. The attributes are registered in
[`semconv/earshot.yaml`](../semconv/earshot.yaml).
Set the `earshot.host` instrumentation scope version to the profile version emitted.
An absent scope version retains 0.1.0 behavior. A different or unrecognized version
does not opt in to profile 0.2.0: its fields are interpreted as 0.2.0 only when the
scope version is exactly `0.2.0`.

| Span                          | Parent | Carries                                                                                                                                                                                                                                                                               |
| ----------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `turn`                        | any    | `earshot.turn.id` (the host's turn ID); optionally `earshot.turn.index`, `earshot.turn.reported_latency_ms` (the host's own measure of the turn's latency, in milliseconds) and `earshot.turn.timeline_continuous` (`false` when frames were dropped or placed late during the turn). |
| `user_speech`, `agent_speech` | `turn` | `earshot.turn.id`; in profile 0.2.0, optionally `earshot.transcript.text`. Its window is one stretch of the caller's or the agent's speech.                                                                                                                                           |
| `stt`, `llm`, `tts`           | `turn` | `earshot.turn.id`, `gen_ai.provider.name`, `gen_ai.request.model`; on `llm`, the `gen_ai.usage.*` counts; in profile 0.2.0, optional provider-metered STT seconds or TTS characters. Its window is one stage request.                                                                 |

- **Turns.** One per `turn` span. User on is the starts of its `user_speech` spans, user
  off the latest of their ends, and agent on the earliest start of its `agent_speech`
  spans. The reported latency is `earshot.turn.reported_latency_ms`. A present value is
  a positive finite OTLP `doubleValue` or `intValue`. Absent, zero, negative, or
  non-finite values are omitted and remain unknown, never 0.
- **The audible gap** is measured on the `continuous_timeline_assumed` basis, since the
  host places its Recording's frames by its wall clock. A turn with
  `earshot.turn.timeline_continuous=false` is `not_measurable` with
  `recorder_timeline_discontinuity`, whatever was measured (a talk-over too), with no
  value, bound or speech edges, which would give the gap back. The call's signals (talk-overs,
  dead air, long silences) come from the audio and still count that turn, as they do for
  Pipecat's detected discontinuity.
- **Stages.** The stage spans are listed on their turn (`turns[].spans`, by role). A
  stage the host did not time has no span: its time is unknown, never 0.
- **Usage.** `llm` spans bill like Pipecat's `llm` (input, cached and output tokens).
  Profile 0.1.0 defines no STT or TTS quantity attributes. Profile 0.2.0 optionally adds
  `earshot.stt.usage.audio_seconds`, a positive finite OTLP `doubleValue` or positive
  `intValue` of provider-metered audio seconds for one STT request. Each report is
  rounded to milliseconds per request, with a one-millisecond minimum for positive
  values. Earshot reprices from that stored millisecond quantity. It also adds
  `earshot.tts.usage.character_count`, a positive OTLP `int64` in the provider's own TTS
  character-metering units for one request. These are request quantities, not cumulative
  counters. Hosts omit unknown values; zero is invalid. Do not infer STT usage from span
  or speech duration, or TTS usage by counting text, bytes,
  Unicode scalar values, or grapheme clusters. The profile 0.2.0 additions do not change
  existing provider, model, stage-boundary, LLM-usage, or turn semantics. When no STT
  request for a provider/model reports seconds, the existing call-level audio estimate
  remains. Hosts must omit a streaming STT session total reported at close from this
  per-request attribute; it is not request usage and cannot replace that estimate. If
  only some requests report seconds, Earshot sums those reported quantities and leaves
  missing requests unknown; it does not add a call-wide estimate over the reported
  requests. Missing TTS counts remain unknown.
- **Transcript.** In profile 0.2.0, a host may attach one nonblank,
  source-provided `earshot.transcript.text` value to each `user_speech` or
  `agent_speech` span, no larger than 64 KiB (65,536 bytes when encoded as UTF-8). For
  `user_speech`, send final transcription only, never interim or partial hypotheses.
  For `agent_speech`, send the words actually spoken in that speech stretch, not
  generated response text that was not spoken. The span name supplies the speaker and
  its interval supplies the transcript line's measured timing. Omit the attribute when
  no text was supplied; an empty or whitespace-only value is invalid. Earshot does not
  derive transcript text from audio or other stage output. Under `full`, each supplied
  line is stored with its span and appears in the Call transcript. Under `metadata_only`,
  the attribute is removed before storage and transcript coverage is
  `not_captured_by_preset`. The host decides whether a call's text may be sent; Earshot
  stores only text it receives. U+0000 is removed from the normalized Call transcript
  for Postgres compatibility; the source span and a full-capture Revision keep the
  received value, subject to capture budgets.
  Profile 0.1.0 does not define this attribute and remains accepted.
- **Recordings.** A host sends its Recording with `POST /v1/recordings` as any producer
  does.
- **The end.** Any span with `earshot.call.ended=true` ends the call, as for any producer;
  the call attributes above (`earshot.agent.name`, `earshot.call.outcome`,
  `earshot.call.tags`) apply.

Give spans fixed IDs per call and turn, so a retried export writes the same spans. A
span is immutable: a re-sent span with other values keeps the first one and counts as
a conflict, so send each span once its values are final.

The profile is a convention, not an allowlist: Earshot keeps whatever else a host adds
to its spans as it does for any producer, under `full` as sent (declared by capture
class in the Revision) and under `metadata_only` without content keys. A host that
must not export text keeps it off its spans.

### Offline conformance checker

Run the standard-library-only checker before exporting a host-profile request:

```sh
earshot check-host-profile request.json
cat request.json | earshot check-host-profile -
```

It prints JSON with `valid`, `host_profile_versions`, `spans_checked`, and `issues`;
each issue has a stable `code`, JSON `path`, and explanation. Exit status is `0` for a
valid request, `1` for a request that fails the profile, and `2` when the input cannot
be read or the command line is invalid. The valid and invalid examples are packaged at
`earshot.host_profile_fixtures`; for example,
`importlib.resources.files("earshot.host_profile_fixtures").joinpath("0.2.0-reported.json")`
locates the reported-value example after installation.

Every issue code:

| Code                                                                                                        | Meaning                                                                        |
| ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `invalid_json`                                                                                              | Input is not strict UTF-8 JSON.                                                |
| `invalid_request`                                                                                           | The root request or `resourceSpans` has the wrong shape.                       |
| `invalid_resource_span`, `invalid_scope_spans`, `invalid_scope_span`, `invalid_scope`, `invalid_scope_name` | An OTLP resource/scope envelope member has the wrong shape.                    |
| `wrong_scope`                                                                                               | A host-profile scope or field uses a scope other than `earshot.host`.          |
| `unsupported_version`                                                                                       | The `earshot.host` scope version is not supported.                             |
| `invalid_spans`, `invalid_span`, `no_host_spans`                                                            | The scope's spans have the wrong shape, or no host spans were supplied.        |
| `unknown_span_name`                                                                                         | A host span name is outside the host-profile vocabulary.                       |
| `snake_case_proto_field`                                                                                    | An OTLP/JSON property uses its original proto snake_case spelling.             |
| `invalid_trace_id`, `invalid_span_id`, `invalid_parent_span_id`                                             | A trace or span ID has the wrong hexadecimal length.                           |
| `missing_parent`, `parent_not_turn`                                                                         | A child has no parent in the request, or its parent is not a host `turn`.      |
| `invalid_span_time`, `span_time_order`                                                                      | A timestamp is not unsigned 64-bit nanoseconds, or the end precedes the start. |
| `invalid_attributes`, `invalid_attribute`, `invalid_any_value`                                              | An attribute list, entry, or OTLP `AnyValue` has the wrong shape.              |
| `duplicate_attribute`                                                                                       | A host-profile attribute appears more than once on a span.                     |
| `wrong_scope_version`                                                                                       | A 0.2.0 field is used under another supported profile version.                 |
| `wrong_span`                                                                                                | A host-profile attribute is attached to a span role that does not define it.   |
| `wrong_attribute_type`                                                                                      | A host-profile value uses the wrong OTLP `AnyValue` kind.                      |
| `invalid_transcript_text`                                                                                   | Transcript text is blank, invalid UTF-8, or larger than 65,536 UTF-8 bytes.    |
| `invalid_turn_reported_latency`                                                                             | Reported latency is not a finite positive double or int64.                     |
| `invalid_stt_audio_seconds`                                                                                 | STT audio seconds are not a finite positive double or int64.                   |
| `invalid_tts_character_count`                                                                               | TTS characters are not a positive JSON-safe int64.                             |
| `missing_turn_id`, `invalid_turn_id`                                                                        | A non-call host span lacks a nonblank `earshot.turn.id`.                       |
| `missing_stt_provider`, `invalid_stt_provider`                                                              | An STT span lacks a nonblank `gen_ai.provider.name`.                           |
| `missing_stt_model`, `invalid_stt_model`                                                                    | An STT span lacks a nonblank `gen_ai.request.model`.                           |

The checker validates one request's OTLP/JSON structure and host-profile fields. It
does not establish that reported values match the live audio, that a transcript is
final or authorized for export, or that the host meets a runtime latency target. It
also ignores non-host scopes and unknown extension attributes.

### Mapping interruption, turn, and stage outcomes

Use source observations and direct span measurements. Configuration describes how a
runtime is set up; it does not say which outcome happened. These mappings add no
host-profile attributes.

| Case                                          | Source fact                                                                                                                                                                                                        | Host-profile mapping                                                                                                                                                                                                                            | Do not infer                                                                                                                                                                                                                           |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Accepted barge-in followed by cancellation    | A direct runtime observation confirms the interruption was accepted; a separate terminal observation says the turn was cancelled.                                                                                  | Preserve `earshot.interruption.accepted` and `earshot.turn.cancelled` as separate events on the `turn` span when each fact was observed. If acceptance was not observed, omit that event; absence is not evidence that acceptance was rejected. | A cancelled turn, speech overlap, or a `barge_in` cancellation reason alone does not prove that an interruption was accepted.                                                                                                          |
| `allow` versus `graceful` interruption policy | The policy mode is source configuration. In the current TVIC runtime, both modes use the same interrupt path; `ignore` is the special case, while `minSpeechMs` and `trimOutputOnInterrupt` are separate settings. | Report the directly observed accepted-interruption event. The host profile does not encode the policy as an outcome.                                                                                                                            | Do not claim different observed behavior for `allow` and `graceful`, or infer an interruption from either policy setting.                                                                                                              |
| Failed versus cancelled turn                  | The runtime directly reports a terminal failure or cancellation.                                                                                                                                                   | Set OTel span status to `ERROR` only for a directly observed failed turn. Preserve `earshot.turn.cancelled` for an observed cancellation; cancellation alone is not an error.                                                                   | OTel `UNSET` is not proof of success or cancellation, and missing `ERROR` is not proof of completion. OTel status describes operation status, not a three-state turn lifecycle.                                                        |
| TTS versus LLM stage timing                   | Each `tts` or `llm` span directly measures that stage's own interval and status.                                                                                                                                   | Retain the stage span's interval and observed status. A missing span or measurement is unknown.                                                                                                                                                 | Do not derive a stage duration from turn latency, total turn time, or first-token delay. A long span is not a “stall” without a directly observed stage failure/timeout or an agreed threshold; this set defines no numeric threshold. |

The host profile's `turn` and stage events are retained in raw span detail. The current
host-span adapter does not normalize arbitrary span events into Call-level interruption
analytics; span-event evidence is available in the span detail response only.

## The calls list: `GET /v1/calls`

Newest first by first evidence, with the 0.2.0 keyset cursor. Each item carries
`projection_state` and the projection's summary. Every filter given must match, and a
filter never matches an unknown (null) value or a Call without a projection.

| Parameter                                            | Matches                                                                                                                                                          |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent`, `outcome`                                   | Exactly.                                                                                                                                                         |
| `tag`                                                | Repeatable, at most 10: the Call has every one.                                                                                                                  |
| `min_reported_latency_ms`, `max_reported_latency_ms` | The reported latency p50.                                                                                                                                        |
| `min_audible_gap_ms`, `max_audible_gap_ms`           | The audible gap p50, over measured turns.                                                                                                                        |
| `min_cost_nano_usd`                                  | The cost lower bound.                                                                                                                                            |
| `max_cost_nano_usd`                                  | Complete costs only, on the lower bound plus the estimate. The page's `excluded_incomplete` counts the Calls left out only because their cost has unknown items. |
| `q`                                                  | Transcript text: see below.                                                                                                                                      |

**Transcript search (`q`).** A substring of any placed transcript line, 3 to 256
characters. `%`, `_` and `\` match themselves. Case is folded for ASCII letters; on
Postgres other scripts fold too. The Project's **current** Capture Preset decides: under
`metadata_only`, `q` is refused with 422 `EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH`, even if
older lines exist. A list request with `q` is `no-store`. Search scans the lines; on
Postgres, `earshot db enable-transcript-index` adds an optional trigram index (see
[production notes](./production.md#transcript-search-index)).

## Span details: `GET /v1/calls/<call_id>/spans/<trace_id>/<span_id>`

One stored span of the Call, for the viewer's LLM, tool and error drawers: its name,
times, status and events, and each attribute with its `semantic_key` and `capture_class`
(LiveKit 1.6.5 to 1.8.x and Pipecat keys read through one table, so `gen_ai.input.messages`
is the LLM input whichever framework sent it). It answers 404
`EARSHOT_SPAN_NOT_FOUND` unless the span's trace belongs to the Call. Content is what the
preset kept when the span arrived. `Cache-Control: no-store`.

## Audio: content, peaks and renditions

| Route                                          | Returns                                                                                                                                                                      | Caching                          |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `GET /v1/recordings/<id>/content`              | The Recording's bytes, with `Range` (206).                                                                                                                                   | as in 0.2.0                      |
| `GET /v1/recordings/<id>/peaks`                | audiowaveform `.dat` v2 peaks (8-bit, 256 source samples per pixel), one channel per channel of the Recording. 404 `EARSHOT_PEAKS_NOT_FOUND` until the analysis stored them. | `private, no-cache`, `ETag`, 304 |
| `GET /v1/recordings/<user_id>/rendition`       | A mono pair's stereo FLAC (user on channel 0), with `Range`. 404 `EARSHOT_RENDITION_NOT_FOUND` otherwise.                                                                    | `no-store`                       |
| `GET /v1/recordings/<user_id>/rendition/peaks` | The pair's peaks as one two-channel `.dat`.                                                                                                                                  | `private, no-cache`, `ETag`, 304 |

Use the paths `projection.media[]` gives rather than building them.

Hosted reads require `earshot:calls:read` and stay scoped to the token's signed Project.
Successful audio and rendition streams use `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`; range errors also use `no-store`. Peaks are private
and revalidated by ETag. A peaks response is limited to 8 MiB, and concurrent media reads
are bounded. A `HEAD` request uses the same authorization and response headers as its
`GET`, without a response body.
Stored audio remains readable after a switch to `metadata_only`; the preset applies to
new evidence and does not retroactively erase a Recording. Earshot does not create
end-user access audit records for these reads; the host proxy owns that audit trail.

## Prices

Built-in prices ship with Earshot: regular, dated list prices for Deepgram, Groq, OpenAI,
Cartesia and ElevenLabs models, each with its source URL. A model with no public price
has none and its usage is `no_price`. Add your own:

```bash
# The project's own price (it wins over a built-in one), here $0.0059 per minute:
earshot prices add --project default --provider deepgram --model nova-3 \
  --unit audio_second --price-nano-usd 5900000 --per-quantity 60 \
  --note "our contract rate"
# A built-in row for every project (operators), for example a newly published price:
earshot prices add --builtin --provider <provider> --model <model-id> \
  --unit output_token --price-nano-usd <price> --source-url <price page>
earshot prices list --project default
```

Or over the API: `GET /v1/prices` (built-ins and the caller's rows, oldest first, keyset
pages) and `POST /v1/prices` with `provider`, `model`, `unit` (`input_token`,
`output_token`, `cached_input_token`, `character`, `audio_second`), `price_nano_usd`,
`per_quantity` (default 1), `effective_from_unix_nano` (a decimal string; `"0"` for
every call), and optional `source_url` and `note`. Provider and model are lower-cased and
must match the usage's spelling (`gen_ai.provider.name`, the model ID). Rows are
append-only: a call uses the latest row effective at or before its first evidence, and a
new row reprices the affected calls in the background.

## Hosted mode

In `hosted_jwt` mode a host reaches the Calls with three scopes of their own, which the
generic incident scopes never grant:

| Scope                  | Operations                                                                                     |
| ---------------------- | ---------------------------------------------------------------------------------------------- |
| `earshot:calls:write`  | `POST /v1/traces`, `POST /v1/recordings`, `PUT /v1/recordings/{recording_id}/content`          |
| `earshot:calls:read`   | Call list/detail, live turns, resolve, dashboard and the project-scoped Recording reads below. |
| `earshot:calls:delete` | `DELETE /v1/calls/{call_id}`                                                                   |

Every other Call surface stays closed in hosted mode and answers 403
`EARSHOT_OPERATION_UNAVAILABLE` for every method: a span's raw attributes
(`/v1/calls/{call_id}/spans/*`), recording metadata, transcript upload, `/v1/prices*` and
`/v1/otlp/stats`. Recording content, waveform peaks and stereo renditions are
project-scoped to the hosted token's signed project ID; no blob-store URL is returned. The
read routes are `GET /v1/recordings/{recording_id}/content`,
`GET /v1/recordings/{recording_id}/peaks`,
`GET /v1/recordings/{recording_id}/rendition` and
`GET /v1/recordings/{recording_id}/rendition/peaks`.
The hosted OpenAPI lists exactly the open operations. Hosted authorization is one closed
table of (method, route) and scope: an operation not in it is refused, whatever the token
grants.

A call's revisions are incidents. A hosted token without `earshot:calls:read` never sees
one: incident list, turn metrics and summaries leave them out, and any incident route that
names one (including a comparison's `known_good_bundle_id`) answers exactly what a missing
incident answers, 404 `EARSHOT_INCIDENT_NOT_FOUND`.
