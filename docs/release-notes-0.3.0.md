# Earshot 0.3.0 release notes

0.3.0 turns each closed Call into a **call view**: its audio with a waveform, the
transcript on the audio's time axis (click a line to seek), per-turn latency as the
framework reports it beside the **audible gap at the agent** measured from the
recording, signals across the call, and its cost. The calls list filters on all of it
and searches transcript text. LiveKit Agents 1.6 to 1.8 now get the same view as Pipecat
1.5. The [CHANGELOG](../CHANGELOG.md) lists every change.

## What's new

- **The call view and the calls list** in the bundled viewer (`/calls`,
  `/calls/<call_id>`) and the API behind them: [the call view](./calls.md).
- **Recording analysis.** Each stored Recording is analysed once, in a child process:
  speech runs with their uncertainty, waveform peaks, and a stereo rendition for a mono
  pair. It needs the new `signals` extra. See
  [ADR 0008](./adr/0008-recording-analysis-and-call-projections.md).
- **Cost.** A lower bound from priced usage, a separate estimate for STT, and a count of
  unknown items, never $0 for "unknown". Built-in regular list prices, and your own per
  Project. See [ADR 0009](./adr/0009-cost-model.md).
- **Stereo recordings**: one file with `channel_speakers: ["user", "agent"]`, as WAV,
  FLAC or Ogg Opus.
- **LiveKit over OTLP**: LiveKit 1.6 to 1.8 spans become Revisions and call views, with
  the played text as the agent's transcript, and LiveKit's own stereo recording.
- **Clients**: Python's `earshot.client` (in the base install) and the new npm package
  `@earshot/node`. See the [quick start](./otlp-quickstart.md).

## Upgrading

1. Back up ([production notes](./production.md#backups)).
2. Install `earshot-observability[server,signals]` with your other extras (the container
   image includes `signals`; it is now 142 MB, from 94 MB).
3. Start the server. It migrates the catalog to `0005_projections` (forward-only),
   queues an analysis for every Recording already stored, and projects every Call that
   has a Revision. Capture Presets are unchanged.

**Changes that can break a 0.2.0 client** (0.2.0 was never published, so these affect
only installs from source):

- A Recording upload with the wrong channel count is refused with
  `EARSHOT_RECORDING_CHANNEL_MISMATCH`, which replaces `EARSHOT_RECORDING_NOT_MONO`.
- An Ogg upload must be Ogg Opus; other Ogg codecs get `EARSHOT_RECORDING_INVALID_OGG`.
- Under `metadata_only`, LiveKit `lk.*` keys that Earshot's key table does not list (for
  example `lk.sip.*`) are now dropped; 0.2.0 kept them as metadata.
- The `livekit` extra allows `livekit-agents>=1.6.5,<1.9`.

## What the tests prove, and what they don't

Verified for this release, on real calls and real audio:

- **One real Pipecat 1.5 call and one real LiveKit Agents 1.8 call** (Deepgram, Groq,
  Cartesia), each into `earshot serve` on Postgres and S3, each reach a call view with
  every Recording analysed, at least one turn's audible gap measured, every measured gap
  between 0 and 15 s with a ± of at most 100 ms (the release's accuracy target), every
  other turn not measurable with its reason, a cost lower bound with an item from each
  provider, and the transcript placed on the recording. In the release run the measured
  ± values were 47 to 81 ms.
- **Both captured calls replayed without providers** give exactly the pinned values in
  CI: every gap and ± to the millisecond, every transcript line on its speaker's speech,
  and the exact cost items.

The limits of those results:

- **The LiveKit call is headless (Tier A).** It runs LiveKit's agent as an in-process
  fake job: no LiveKit server, no room, no WebRTC. It proves the path from LiveKit's
  spans and its recorder to the call view (the plumbing), not the accuracy of a gap
  measured on audio that crossed a real room.
- **The audible gap is measured at the agent's recorder.** It is not what a caller heard
  over a phone line or a network.
- **Accuracy on natural human speech is unverified.** Both real calls use synthesized
  caller speech, and no hand-labelled recording of a human caller has been measured. The
  ± is the analysis's own estimate of its edge uncertainty, not an error measured against
  labels.
- **Pipecat's first turn is often not measurable.** Pipecat's recorder can insert time
  into its tracks; when the span anchors show it did inside a gap, the turn shows only an
  upper bound ("≤ 1.78 s"), never a corrected value.
- **Telephony audio (8 kHz)** gives less precise speech edges, and larger ± values.
- **The player** is tested with a stand-in media clock, because the test DOM plays no
  audio. Playback in Chrome and Safari, including Safari with LiveKit's Ogg Opus, is a
  manual release check.

## Not in 0.3.0

OTLP over gRPC; call, recording and price routes for hosted JWTs; LiveKit with a real
server and room (Tier B); a price editor in the viewer; ranked full-text search;
diarization; pricing for realtime (speech-to-speech) models; per-project signal
thresholds.
