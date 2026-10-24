# Calls: the call view, the calls list, cost and search

Since 0.3.0, every closed Call gets a **call view**: its audio with a waveform, the
transcript on the audio's time axis, per-turn latency as the framework reports it beside
the **audible gap at the agent** measured from the recording, signals across the call,
and its cost. The calls list filters on all of these and searches transcript text.

The bundled viewer shows them at `/calls` and `/calls/<call_id>`. This page describes
what the server computes and the API the viewer reads; the
[OTLP quick start](./otlp-quickstart.md) shows how to send a call. The design is in
[ADR 0008](./adr/0008-recording-analysis-and-call-projections.md) (analysis and
projections) and [ADR 0009](./adr/0009-cost-model.md) (cost). The exact schemas are in
[`spec/backend-api.openapi.json`](../spec/backend-api.openapi.json).

## What a call needs

- **Spans** from Pipecat 1.5 or LiveKit Agents 1.6 to 1.8, keyed by a Session Key. They
  give the turns, the reported latency, the transcript and the usage.
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
holding U+0000 is absent, and U+0000 is removed from transcript text, so it is never
stored.

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
| `turns[]`                                       | One per framework turn: its window, `reported_latency_ms`, `audible_gap`, and its `spans[]` for the detail drawers.                                                                                                                                       |
| `signals`                                       | See [signals](#signals).                                                                                                                                                                                                                                  |
| `cost_items[]`                                  | Each priced, estimated or unknown item.                                                                                                                                                                                                                   |
| `transcript[]`                                  | The placed transcript. See [transcript placement](#transcript-placement).                                                                                                                                                                                 |

Unknown is `null`, never `0`. The response is `Cache-Control: no-store` with a strong
`ETag`; `If-None-Match` with it answers 304.

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

| `reason`                          | Meaning                                                                                                                       |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `recorder_timeline_discontinuity` | The recorder inserted time inside the gap (Pipecat's recorder only ever inserts time), so the raw gap is only an upper bound. |
| `no_agent_audio`                  | No agent speech near the framework's agent-on point.                                                                          |
| `no_clear_pairing`                | No user speech ends before the agent's, or other agent speech lies between.                                                   |
| `not_one_timeline`                | The Recordings are not on one timeline (different starts, durations over 20 ms apart, or an ambiguous mono pair).             |
| `no_recording`                    | The Revision has no Recording.                                                                                                |
| `analysis_not_stored`             | The analysis is pending, failed or unavailable; `analysis_state` says which.                                                  |
| `boundary_uncertainty_too_high`   | The combined speech-edge uncertainty exceeds 100 ms, so the gap is withheld.                                                  |

`basis` is `anchors_checked` (Pipecat: the turn's span anchors were checked against the
audio for a discontinuity) or `continuous_timeline_assumed` (LiveKit, whose recorder
places frames by wall clock). `user_end_ms` and `agent_start_ms` are the paired speech
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

| Item `status`  | When                                                                                |
| -------------- | ----------------------------------------------------------------------------------- |
| `priced`       | The usage was reported, and a price matches.                                        |
| `no_price`     | The usage was reported, and no price matches.                                       |
| `not_reported` | A request left its usage unknown (one item per request).                            |
| `estimated`    | STT seconds, which no framework reports: the user audio's duration, once per model. |

`lower_bound_nano_usd` sums the priced items; `estimated_nano_usd` sums the estimated
ones; `unknown_items` counts the rest. A cost is `complete` when it has a lower bound and
no unknown item. The viewer shows `$0.0090` for a complete cost, else
`≥ $0.0046 · 2 unknown`, and the estimate apart as `+ ~$0.0042 (estimated)`. Money is
integer nano-USD (10⁻⁹ USD). [ADR 0009](./adr/0009-cost-model.md) lists which spans
bill and why.

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
(LiveKit 1.6 to 1.8 and Pipecat keys read through one table, so `gen_ai.input.messages`
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

In `hosted_jwt` mode every `/v1/calls*`, `/v1/recordings*` and `/v1/prices*` route
answers 403 `EARSHOT_OPERATION_UNAVAILABLE`, and the hosted OpenAPI omits them.
