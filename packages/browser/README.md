# @earshot/browser

The **client-side voice capture kernel**. It runs in the browser (or any
DOM-ish/Node runtime), observes the live W3C media APIs, and emits a versioned
`CapturePayload` in the **exact shape the earshot server engines consume** — then
delivers it to the backend's capture endpoint. You capture on the client and
diagnose on the server.

- `RTCPeerConnection.getStats()` snapshots → `analyze_webrtc_stats`
- `AudioContext` state / latency / render position / sink + `getUserMedia` /
  device / permission lifecycle → `analyze_audio_graph`
- a per-session **W3C trace-context** (`traceparent`) so client capture and
  server spans correlate
- a bounded, authenticated **transport** that POSTs to `POST /v1/capture` and
  records any undelivered batch as coverage

> Server counterparts (this SDK matches their shapes):
> `packages/sdk-python/src/earshot/engines/webrtc.py`, `.../engines/device.py`,
> and the endpoint in `packages/sdk-python/src/earshot/api.py`.

## Status

**Not published. Validated on Chrome only.**

- `private: true`, unpublished. Consume it from this workspace; whether it ever
  goes to npm is the maintainer's call, not this package's.
- The capture, bounding and delivery logic is unit-tested against mocked W3C
  APIs and a mocked `fetch`. Every environment dependency (clock, interval
  scheduler, randomness, `fetch`, and each W3C object) is injected via small
  structural interfaces (`src/types.ts`, `src/testing/fakes.ts`), which is what
  makes the logic fully exercisable off-device.

### What a real browser has confirmed

One on-device pass has been run: **Chrome 150.0.7871.186 on macOS 26.3**, driving
a real loopback `RTCPeerConnection` (an oscillator through a
`MediaStreamAudioDestinationNode`, so no microphone permission) plus a real
`AudioContext`, then delivering the result to a live `POST /v1/capture`.

- `RTCStatsReport` member names and units match what this package expects — the
  server accepted the real payload with **zero** rejected stats, stat members,
  device events or device members, so client and server agree on the shape.
- `media-playout` (`RTCAudioPlayoutStats`) and `getOutputTimestamp()` are both
  available in that build and produced real readings.
- **The allowlist holds against real host-identifying material.** That session's
  raw `getStats()` genuinely contained `certificate.base64Certificate`, a DTLS
  `certificate.fingerprint`, and `usernameFragment`/`port` on both local and
  remote candidates. None of those values survived into the `CapturePayload`,
  and the stored incident contained no forbidden key, no IPv4/IPv6 literal, and
  no base64 certificate material.
- The `webrtc.audio_decode_time` coverage note is correct on real Chrome: audio
  decode time genuinely is not exposed (`totalDecodeTime` is video-only).

### Still unvalidated

- **Safari and Firefox** — not exercised at all; vendor stat coverage varies.
- **`getUserMedia` and the Permissions API `microphone` descriptor** — the pass
  used a synthesised audio source, so the real microphone-permission path is
  still only covered by unit tests.
- `AudioContext` `sinkchange` / `setSinkId` and `outputLatency`.

Where a browser turns out not to expose a signal, the kernel records an explicit
coverage note for it (see **Coverage** below) rather than guessing — so an
unvalidated platform degrades into a stated unknown, not a wrong number.

## Usage

```ts
import { createBrowserRecorder, createCaptureTransport } from "@earshot/browser";

const recorder = createBrowserRecorder({ sessionId }); // clock/scheduler/random are injectable

recorder.attachPeerConnection(pc, { intervalMs: 1000 });
recorder.attachAudioContext(audioContext, { renderTimingIntervalMs: 1000 });
await recorder.observeMediaDevices(navigator.mediaDevices, {
  permissions: navigator.permissions,
});
await recorder.requestMicrophone(navigator.mediaDevices, { audio: true });

const transport = createCaptureTransport({
  endpoint: "https://your-earshot-host/v1/capture", // required; no default
  apiKey: projectApiKey, // or omit and use the viewer session cookie + csrfToken
  coverage: recorder, // an undelivered batch becomes coverage on the next one
  onFailure: (failure) => metrics.increment("earshot.capture.dropped", failure),
});

// periodically POST what we've captured; drain() empties the buffers
setInterval(() => void transport.send(recorder.drain()), 10_000);

await transport.flush(); // before unload
recorder.stop(); // clears intervals + listeners
transport.stop(); // abandons the queue, recording each loss as coverage
```

### `CapturePayload` (what `drain()` returns)

```ts
{
  captureVersion: 1,                                // the wire format the server gates on
  sessionId: string,
  traceContext: { traceparent, traceId, spanId },   // W3C; JOINED from the app when supplied
  clockDomain: { id, kind, unit, uncertaintyMs, wallOriginMs }, // the browser clock these timestamps belong to
  snapshots:  [ { timestamp_ms, stats: { [id]: { ...allowlisted RTCStats members } } } ],
  deviceEvents: [ { type, timestamp_ms, ...members } ],
  coverage: [ { signal, availability, reason, droppedCount? } ], // explicit loss, never silent
}
```

`snapshots` feeds `analyze_webrtc_stats`; `deviceEvents` feeds
`analyze_audio_graph`. `src/roundtrip.test.ts` asserts the shapes line up with
those two functions' normalisers.

Every `timestamp_ms` is a **raw** reading of the injected monotonic clock (e.g.
`performance.now()`), never rebased to zero. `clockDomain.id` is stable for the
recorder's lifetime, so the server records these readings as `monotonic_time_nano`
inside their **own** browser `ClockDomain` — a browser timestamp is never treated
as a server-clock observation. Because there is no calibration between the two
clocks by default, cross-clock latency stays honestly _unavailable_ until a caller
supplies a `ClockRelation`; `wallOriginMs` (from `performance.timeOrigin`) is what
such a calibration aligns.

## Transport — `POST /v1/capture`

`createCaptureTransport({ endpoint, ... })` posts drained payloads to the earshot
backend's capture endpoint. That endpoint exists: it is implemented in
`packages/sdk-python/src/earshot/api.py` and published in
`spec/backend-api.openapi.json`. It accepts this payload, enforces its own
allowlist over every stat and device-event member, and stores the batch as a
governed incident (`framework: browser_capture`) whose facts sit in the browser
clock domain you declared.

- **Versioned.** `captureVersion` travels in the body, not the URL, so client and
  server evolve independently of the shared `/v1` route. A server that does not
  govern the version answers `EARSHOT_UNSUPPORTED_CAPTURE_VERSION`, which this
  client treats as permanent (retrying would only repeat the answer).
- **Authenticated, never hardcoded, never logged.** `endpoint` is required and
  has no default; `apiKey` (bearer) or the viewer session cookie plus
  `csrfToken` authenticate the call — the endpoint is a normal `/v1` route, so a
  cookie-authenticated POST needs its CSRF token. The credential is written into
  the `Authorization` header and nowhere else: not into failure objects, not into
  error messages, and not into any console call (this package makes none).
  `src/transport.test.ts` asserts that with a sentinel key.
- **Bounded.** One delivery in flight at a time, in order; a hard queue cap
  (`maxQueuedPayloads`, default 8) that drops the **oldest** payload on overflow;
  a bounded number of attempts (`maxAttempts`, default 3) with doubling backoff
  up to `maxRetryBackoffMs`. Only failures that could plausibly succeed later
  (transport error, 408, 425, 429, 5xx) are retried.
- **No silent drops.** Every abandoned payload is reported to `onFailure` **and**
  recorded on the `coverage` sink as `capture.upload` / `partial` /
  `upload_failed_payload_dropped` (or `upload_queue_overflow_oldest_dropped`),
  carrying the number of observations lost. The dropped payload's own coverage
  notes are forwarded too, so the gaps it was already declaring survive the
  failure. Pass `coverage: recorder` and those notes appear in the next
  `drain()`.
- **Duplicate-safe.** The server derives each incident's identity from the
  batch's content, so a retry after an unknown outcome resolves to the incident
  the first delivery created (`200`) instead of a second copy of the same
  evidence (`201` is a genuinely new batch).

## What is instrumented, and what the platform actually provides

Nothing below is derived from a signal the platform does not expose.

| Signal                             | Source                                                                                                                               | Note                                                                                                                        |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Loss, jitter, round-trip time      | `inbound-rtp` / `remote-inbound-rtp` / `candidate-pair`                                                                              | deltas over consecutive snapshots, computed server-side                                                                     |
| Jitter buffer depth and behaviour  | `jitterBufferDelay`/`EmittedCount`/`TargetDelay`/`MinimumDelay`/`Flushes`                                                            | the receive queue between network and decoder                                                                               |
| Concealment and rate adaptation    | `concealedSamples`, `silentConcealedSamples`, `concealmentEvents`, `insertedSamplesForDeceleration`, `removedSamplesForAcceleration` | what the decoder had to invent or stretch                                                                                   |
| Received → decoded time            | `inbound-rtp.totalProcessingDelay`                                                                                                   | averaged per emitted sample server-side                                                                                     |
| **Per-frame audio decode time**    | **not available**                                                                                                                    | `totalDecodeTime`/`framesDecoded` are **video-only** in webrtc-stats                                                        |
| Playout delay and render under-run | `media-playout` (`RTCAudioPlayoutStats`)                                                                                             | `totalPlayoutDelay`/`totalSamplesCount`; `synthesizedSamplesDuration` grows only when the output device had to invent audio |
| Render queue depth                 | `AudioContext.currentTime - getOutputTimestamp().contextTime`                                                                        | audio rendered by the graph but not yet played; an **estimate**                                                             |
| Output / base latency              | `AudioContext.outputLatency` / `baseLatency`                                                                                         | `outputLatency` is a W3C **estimate**; `baseLatency` is deterministic                                                       |
| `AudioContext` state               | `statechange` (`running`/`suspended`/`closed`/iOS `interrupted`)                                                                     | a suspended context is silence                                                                                              |
| Output route change                | `sinkchange` + `AudioContext.sinkId`                                                                                                 | the sink id is hashed before it leaves the client                                                                           |
| Sample-rate mismatch               | `AudioContext.sampleRate` vs `MediaTrackSettings.sampleRate`                                                                         | claimed only when both are reported and they differ                                                                         |
| Permission / device lifecycle      | Permissions API, `getUserMedia`, `devicechange`, track `ended`                                                                       | device ids are hashed                                                                                                       |

## Coverage — what the browser could _not_ observe

`coverage[]` is never a formality. Alongside buffer overflow, `getStats()`
failures and skipped overlapping samples, the kernel emits a note for each
render-path signal the running platform does not provide:

| Signal                     | Availability   | Reason                                   |
| -------------------------- | -------------- | ---------------------------------------- |
| `audio.render_timing`      | `not_observed` | `getoutputtimestamp_unavailable`         |
| `audio.render_timing`      | `partial`      | `output_timestamp_unpopulated`           |
| `webrtc.audio_decode_time` | `not_observed` | `decode_time_is_video_only_in_w3c_stats` |
| `webrtc.processing_delay`  | `not_observed` | `member_not_exposed`                     |
| `webrtc.playout`           | `not_observed` | `media_playout_stat_not_exposed`         |
| `capture.upload`           | `partial`      | `upload_failed_payload_dropped`          |
| `capture.coverage`         | `partial`      | `coverage_buffer_overflow`               |

The server records these under a `browser.` prefix so the browser's claim about
what it saw can never overwrite what an engine derived server-side. One honest
limitation: `droppedCount` has no field on the v1alpha1 `Coverage` record, so the
gap is stored while the count is only returned in the capture response.

**Bounds & honesty.** The snapshot/event buffers are bounded (`maxSnapshots` /
`maxDeviceEvents`); on overflow the **oldest** observation is dropped and the loss
is recorded in `coverage`, never lost silently. `getStats()`/permission errors and
skipped overlapping samples are likewise recorded as coverage. Caller-supplied
notes (`recorder.recordCoverage`) are merged by signal and hard-capped, so a
persistently failing uploader cannot grow the buffer without limit — the overflow
is itself a coverage note. An invalid polling interval (`<= 0`, `NaN`, `Infinity`)
is rejected with a clear error.

**Trace join.** Pass `{ traceparent }` (or a full `traceContext`) to join the
application's existing trace; the recorder only mints its own when none is supplied
and never overwrites the app's `traceparent`. The payload's trace context is
validated by the server and returned as `trace_id` on the capture response for
correlation; it is not yet attached to the stored incident's individual facts,
because the server's fact-recording seam does not carry trace ids.

## Privacy posture — metadata only

- **No audio is ever read or retained.** The observed APIs expose counters,
  states and timings only; the kernel never touches audio samples.
- **No raw device identity leaves the client.** Device labels/ids and
  `AudioContext.sinkId` are replaced with opaque, **per-session salted** hashes
  (`dev_…` / `sink_…`). The salt is random per recorder, so hashes are not
  linkable across sessions and are not a stable fingerprint. Raw labels are
  never read into an event.
- **`getStats` is filtered by an exact allowlist, not a denylist.** For each
  governed stat type the normaliser copies **only** the specific members the
  server engine reads (the `inbound-rtp` counters above, `candidate-pair`
  `currentRoundTripTime`/`selected`, `transport` `iceState`, `local-candidate`
  `networkType`, the `media-playout` render counters, plus
  `type`/`id`/`kind`/`timestamp`). Every other member — and every stat type the
  server does not consume — is dropped whole. So `base64Certificate`, DTLS
  `fingerprint`, `usernameFragment`, candidate
  `address`/`ip`/`port`/`relatedAddress`/`url`, `trackIdentifier`,
  `decoderImplementation` and anything else cannot leak by omission. Retained
  strings are length-bounded. `src/webrtc.test.ts` seeds a report with each class
  of host-identifying member and asserts none survive `JSON.stringify(drain())`.
- **The server does not trust this allowlist either.** `POST /v1/capture`
  re-derives it from scratch and drops anything outside its own governed set
  before a value reaches an engine, reporting what it refused in the response and
  as coverage. Two independent allowlists, either of which is sufficient.
- **The trace-context carries no secrets** — `traceId`/`spanId` are random
  correlation handles only.

## W3C-correctness

A member that is **absent** on a source stat is **omitted** from the snapshot —
it is never coerced to `0`. The server engines depend on this (a missing counter
is _unknown_, not a measurement). `outputLatency` and the render queue depth are
W3C _estimates_ and the server keeps that distinction (`baseLatency` is
`measured`). A `getOutputTimestamp()` result that is missing, non-finite, or
ahead of the graph clock is reported as unknown, never as a zero-depth queue.

## Develop

```bash
pnpm --filter @earshot/browser build      # tsc -> dist (excludes tests + fakes)
pnpm --filter @earshot/browser typecheck  # tsc --noEmit (incl. tests)
pnpm --filter @earshot/browser test       # vitest run (mocked W3C APIs + fetch)
```
