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

For both direct transport examples below, use this bounded recovery helper. It
retries retained requests and resends the exact terminal payload after an
earlier sequence that blocked it is accepted or permanently rejected. Call it
again after a retry delay while retries remain; the transport and payload must
stay alive until the retry count reaches zero.

```ts
import type {
  CaptureDeliveryResult,
  CapturePayload,
  EarshotCaptureTransport,
} from "@earshot/browser";

interface FinalCaptureState {
  delivery: CaptureDeliveryResult;
  acceptedObserverFailed: boolean;
  pendingExactRetryCount: number;
}

async function retryFinalCapture(
  transport: EarshotCaptureTransport,
  payload: CapturePayload,
  previous: FinalCaptureState,
): Promise<FinalCaptureState> {
  let flush = await transport.flush();
  let retry = flush.retryOutcomes.find(
    (outcome) =>
      outcome.sessionId === payload.sessionId &&
      outcome.drainSequence === payload.drainSequence,
  );
  let delivery = retry?.delivery ?? previous.delivery;
  let acceptedObserverFailed =
    previous.acceptedObserverFailed ||
    delivery.acceptedObserverFailed === true ||
    flush.acceptedObserverFailed;
  const predecessorResolved =
    flush.pendingExactRetryCount === 0 &&
    flush.retryOutcomes.some(
      (outcome) =>
        outcome.sessionId === payload.sessionId &&
        outcome.drainSequence !== undefined &&
        payload.drainSequence !== undefined &&
        outcome.drainSequence < payload.drainSequence &&
        (outcome.delivery.delivered || outcome.delivery.failure?.retryable === false),
    );

  if (
    !delivery.delivered &&
    previous.delivery.failure?.kind === "blocked" &&
    predecessorResolved
  ) {
    // The transport declares any permanently rejected predecessor as resync
    // coverage before accepting this terminal sequence.
    const terminalRetry = await transport.send(payload);
    flush = await transport.flush();
    retry = flush.retryOutcomes.find(
      (outcome) =>
        outcome.sessionId === payload.sessionId &&
        outcome.drainSequence === payload.drainSequence,
    );
    delivery = retry?.delivery ?? terminalRetry;
    acceptedObserverFailed ||=
      terminalRetry.acceptedObserverFailed === true ||
      delivery.acceptedObserverFailed === true ||
      flush.acceptedObserverFailed;
  }

  const state = {
    delivery,
    acceptedObserverFailed,
    pendingExactRetryCount: flush.pendingExactRetryCount,
  };
  const replaceableFinal413 =
    !state.delivery.delivered &&
    state.delivery.status === 413 &&
    payload.end !== undefined;
  if (state.pendingExactRetryCount === 0 && !replaceableFinal413) transport.stop();
  return state;
}
```

```ts
import { createBrowserRecorder, createCaptureTransport } from "@earshot/browser";

const recorder = createBrowserRecorder({ sessionId, captureVersion: 2 });

recorder.attachPeerConnection(pc, { intervalMs: 1000 });
recorder.attachAudioContext(audioContext, { renderTimingIntervalMs: 1000 });
await recorder.observeMediaDevices(navigator.mediaDevices, {
  permissions: navigator.permissions,
});
await recorder.requestMicrophone(navigator.mediaDevices, { audio: true });

const transport = createCaptureTransport({
  endpoint: "/api/earshot/v1/capture", // same-origin authenticated host BFF
  projectId, // required assertion; the BFF derives scope from its cookie
  getAuthContextId, // dynamic, opaque host scope version; rotates on grant changes
  csrfToken, // supplied by the host application for cookie-authenticated POSTs
  coverage: recorder, // an undelivered batch becomes coverage on the next one
  onFailure: (failure) => metrics.increment("earshot.capture.dropped", failure),
});

// Periodic observation drains during the call.
const timer = setInterval(() => void transport.send(recorder.drain()), 10_000);

// When the application knows the call really ended:
clearInterval(timer);
const finalPayload = recorder.endCall();
const initialDelivery = await transport.send(finalPayload);
let finalization = await retryFinalCapture(transport, finalPayload, {
  delivery: initialDelivery,
  acceptedObserverFailed: initialDelivery.acceptedObserverFailed === true,
  pendingExactRetryCount: 0,
});
if (!finalization.delivery.delivered || finalization.pendingExactRetryCount > 0) {
  console.error("The final capture is unresolved; retain the transport and payload.");
}
if (finalization.acceptedObserverFailed) {
  console.error("A browser acceptance observer failed; check the host delivery state.");
}
// On a later user retry or scheduled retry, call retryFinalCapture again with
// this same finalPayload and finalization state. Do not discard either while
// pendingExactRetryCount is nonzero.
```

A terminal `413` is a permanent rejection of that body, but its v2 drain sequence
is still unapplied. Keep the transport open and retry that same session and
`drainSequence` with a smaller terminal payload; preserve its trace, clock, and
`end` fields. Mark omitted observations with the accepted `capture.upload` /
`upload_failed_payload_dropped` coverage reason and dropped count. The server can
accept the replacement at that sequence. If the cumulative journal or another
configured capture limit still rejects the smaller marker with `413`, surface
that as unresolved and let the host repair or deletion flow handle it.

The private browser voice example provides
`CaptureSession.retryFinalWithReducedPayload()`. Its default replacement keeps
the terminal metadata and existing coverage, omits snapshots and device events,
and adds a `capture.upload` coverage note. A host can instead pass an ordered
subset of those observations when that produces a sufficiently smaller request.
The method checks that the replacement is smaller and preserves the original
session, sequence, terminal timestamp, and observation order.

The browser transport uses the host application's session cookie and CSRF token.
It has no project API key or service-JWT option. The BFF derives project scope and
authenticates its Earshot request server-side. For every request, the BFF must
compare both `x-earshot-project-id` and `x-earshot-auth-context-id` with the
project and opaque auth-context version derived from the current cookie. Those
headers are assertions, never authority. `getAuthContextId()` must return a
nonempty, non-secret opaque value that changes whenever the effective user or
project grant changes; do not put a raw user id or credential in it. A transport
captures the initial value, sends it on every request, refuses sends after it
changes, and keeps ambiguous v2 requests bound to that original scope. Create a
new transport for a new effective scope. A blocked failure sets
`authContextChanged: true`.

For continuous calls, opt in to v2 and seal a completed call through the host's
authenticated BFF. The transport reports the server's opaque call id on each
accepted response, including when an earlier ambiguous request succeeds on a later
retry:

```ts
const recorder = createBrowserRecorder({ sessionId, captureVersion: 2 });
const transport = createCaptureTransport({
  endpoint: "/api/earshot/v1/capture",
  projectId,
  getAuthContextId,
  csrfToken,
  onAccepted: (acknowledgement) => ui.captureAccepted(acknowledgement),
});

// Periodic drains use recorder.drain(); close the actual call explicitly.
const finalPayload = recorder.endCall();
const initialDelivery = await transport.send(finalPayload);
let finalization = await retryFinalCapture(transport, finalPayload, {
  delivery: initialDelivery,
  acceptedObserverFailed: initialDelivery.acceptedObserverFailed === true,
  pendingExactRetryCount: 0,
});
if (!finalization.delivery.delivered || finalization.pendingExactRetryCount > 0) {
  console.error("The final capture is unresolved; retain the transport and payload.");
}
if (finalization.acceptedObserverFailed) {
  console.error("A browser acceptance observer failed; check the host delivery state.");
}
// On a later retry, keep calling retryFinalCapture with the same payload and
// returned state until pendingExactRetryCount reaches zero.
```

`onAccepted` is awaited before `send()` resolves, so an asynchronous browser
observer finishes before the caller continues. `acceptedObserverFailed: true`
means Earshot accepted the drain but that browser callback failed. The optional
`onAcceptedFailure` callback receives the same scoped acknowledgement after such
a failure, including on a later `flush()` retry. `flush()` returns each retry's
delivery outcome and `pendingExactRetryCount`; a remaining retry is held only in
memory, so keep the host call unresolved and arrange repair before discarding the
transport. The browser callback is not the
source of truth for sealing: the BFF must persist the accepted
`call_id` and finality to its durable host-owned mapping/outbox before it returns
the response to this page. That server-side write prevents a lost browser
response from losing the call id or stranding a durable capture slot. The browser
does not derive the hosted call id from its raw `sessionId`.

The host worker seals the outbox entry through its authenticated server path at
`/v1/live/sessions/{call_id}/seal`. A seal response can be lost after Earshot has
stored the final artifact and dropped its live buffer; repeating the seal can then
return `404 EARSHOT_SESSION_NOT_LIVE`. On an ambiguous result, query both the
project-scoped `GET /v1/incidents?session_id=<call_id>` and
`GET /v1/live/sessions`. A matching `finality: "final"` artifact alone does not
prove the durable capture slot was released: Earshot may have stored the artifact
before a journal acknowledgment failure returned 503. If the call remains in the
live list, retry the same idempotent seal even when the final artifact exists.
Complete the outbox only when the matching final artifact is present and the call
is absent from the live list. If neither is visible, or either read is unavailable,
keep the entry pending for reconciliation; do not treat the 404 alone as proof
that sealing failed.

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

Continuous capture is opt-in with `createBrowserRecorder({ sessionId,
captureVersion: 2 })`. Each drain then carries a stable session id and a monotonic
`drainSequence`; pair that recorder with its capture transport. If the transport
abandons a drain, the next v2 POST includes a `resync` range so later drains can
continue. Version 1 remains the default and treats each payload as a separate
incident.

Every `timestamp_ms` is a **raw** reading of the injected monotonic clock (e.g.
`performance.now()`), never rebased to zero. `clockDomain.id` is stable for the
recorder's lifetime, so the server records these readings as `monotonic_time_nano`
inside their **own** browser `ClockDomain` — a browser timestamp is never treated
as a server-clock observation. Because there is no calibration between the two
clocks by default, cross-clock latency stays honestly _unavailable_ until a caller
supplies a `ClockRelation`; `wallOriginMs` (from `performance.timeOrigin`) is what
such a calibration aligns.

## Transport — `POST /v1/capture`

`createCaptureTransport({ endpoint, projectId, getAuthContextId, ... })` posts drained payloads to the earshot
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
- **Authenticated through the host.** `endpoint` is required and has no default.
  Configure it as the host's authenticated BFF route; the transport uses the
  same-origin session cookie and sends the supplied `csrfToken` on POST. Browser
  code never receives project API keys or service JWTs.
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
- **Continuous recovery.** For v2 payloads, an abandoned sequence (including an
  evicted queued payload) is declared as `resync` on the next payload for that
  session. The range covers skipped sequence numbers, and the server clips any
  prefix it already committed before recording the remaining loss. Ambiguous
  retryable failures retain the exact request body and retry it before advancing;
  `flush()` makes one bounded retry of pending requests. The recovery ledger is
  bounded to 32 sessions by default (`maxRecoverySessions`); pair one transport
  with one recorder. If a new session would exceed the bound, its failed payload
  cannot be retained and future untracked v2 sessions fail closed on this
  transport. Sessions with already retained retry/resync state can finish that
  recovery; `flush()` retries retained exact requests once and returns each
  outcome plus the number still pending. Keep the transport and terminal payload
  available while any exact retries remain; once a blocked predecessor is
  accepted or permanently rejected, resend the same terminal payload so the
  server can apply it and observe `call_ended`. When rejected, the transport
  declares the missing sequence range before that final payload.
  Recovery state exists only in this
  transport's memory, so the host must reconcile an unretained or still-pending
  failure before treating that call as delivered. `failure.resyncTracked` and
  `failure.retryRetained` report which state was kept.
- **Seal handoff.** Accepted v2 responses expose only `callId` and `finalized`
  alongside delivery status. The acknowledgement also carries the bound
  `projectId` and `authContextId`. `onAccepted` reports it when a later retry
  succeeds and is awaited before `send()` resolves. If the observer throws,
  `acceptedObserverFailed` is set, `onAcceptedFailure` receives the acknowledgement,
  and `flush()` reports that condition too. The host BFF must persist the call id
  to its durable mapping/outbox before returning its response; the browser
  observer is advisory.
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
what it saw can never overwrite what an engine derived server-side, and it keeps
`droppedCount` as `dropped_count` on the stored coverage note, so how much was lost
is part of the artifact rather than only of the upload's acknowledgement.

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
and never overwrites the app's `traceparent`. The server records that context on the
facts it derives from the batch (`trace_id` / `span_id`), so the join is a property
of the stored incident and not only of the capture response. `traceparent` and the
`traceId`/`spanId` beside it must agree: a payload whose two spellings disagree is
refused (`422 EARSHOT_INCOHERENT_TRACE_CONTEXT`) rather than resolved by guessing.

**Ordering.** Every buffer here is append-only against one monotonic clock, so a
drained batch is ordered by construction. The server relies on that and refuses a
batch whose `timestamp_ms` readings move backwards
(`422 EARSHOT_CAPTURE_NON_MONOTONIC`) — normalizing one would report an observation
at a coordinate the browser never read, and difference the cumulative `getStats`
counters over a negative interval.

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
