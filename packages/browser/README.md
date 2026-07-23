# @earshot/browser

The **client-side voice capture kernel**. It runs in the browser (or any
DOM-ish/Node runtime), observes the live W3C media APIs, and emits a
`CapturePayload` in the **exact shape the earshot server engines consume** — so
you capture on the client and diagnose on the server.

- `RTCPeerConnection.getStats()` snapshots → `analyze_webrtc_stats`
- `AudioContext` state / latency / sink + `getUserMedia` / device / permission
  lifecycle → `analyze_audio_graph`
- a per-session **W3C trace-context** (`traceparent`) so client capture and
  server spans correlate

> Server engines (do not change; this SDK matches their shapes):
> `packages/sdk-python/src/earshot/engines/webrtc.py` and `.../device.py`.

## STATUS: scaffolding — NOT yet browser-validated

**The capture logic is unit-tested against mocked W3C APIs, but has NOT been run
against a real browser / WebRTC / Web Audio runtime.** The kernel is written
entirely against small structural interfaces (see `src/types.ts`) and every
environment dependency — clock, interval scheduler, randomness, and each W3C
object — is injected, so the mapping/normalisation logic is fully exercised
(`src/testing/fakes.ts`). What still needs real-runtime validation:

- that a browser's `RTCStatsReport` member names/units match the fixtures used
  here (they follow the W3C stats spec, but vendors vary);
- `AudioContext` `sinkchange` / `outputLatency` availability (partial support);
- the Permissions API `microphone` descriptor across browsers.

Treat this as the structurally-complete client half of the capture→diagnose
contract, pending an on-device conformance pass.

## Usage

```ts
import { createBrowserRecorder } from "@earshot/browser";

const recorder = createBrowserRecorder({ sessionId }); // clock/scheduler/random are injectable

recorder.attachPeerConnection(pc, { intervalMs: 1000 });
recorder.attachAudioContext(audioContext);
await recorder.observeMediaDevices(navigator.mediaDevices, {
  permissions: navigator.permissions,
});
await recorder.requestMicrophone(navigator.mediaDevices, { audio: true });

// periodically POST what we've captured; drain() empties the buffers
await fetch("/capture", {
  method: "POST",
  headers: recorder.injectTraceHeaders({ "content-type": "application/json" }),
  body: JSON.stringify(recorder.drain()),
});

recorder.stop(); // clears intervals + listeners
```

### `CapturePayload` (what `drain()` returns)

```ts
{
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
clocks by default, cross-clock latency stays honestly *unavailable* until a caller
supplies a `ClockRelation`; `wallOriginMs` (from `performance.timeOrigin`) is what
such a calibration aligns.

**Bounds & honesty.** The snapshot/event buffers are bounded (`maxSnapshots` /
`maxDeviceEvents`); on overflow the **oldest** observation is dropped and the loss
is recorded in `coverage`, never lost silently. `getStats()`/permission errors and
skipped overlapping samples are likewise recorded as coverage. An invalid polling
interval (`<= 0`, `NaN`, `Infinity`) is rejected with a clear error.

**Trace join.** Pass `{ traceparent }` (or a full `traceContext`) to join the
application's existing trace; the recorder only mints its own when none is supplied
and never overwrites the app's `traceparent`.

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
  server engine reads (e.g. `inbound-rtp` counters, `candidate-pair`
  `currentRoundTripTime`/`selected`, `transport` `iceState`, `local-candidate`
  `networkType`, plus `type`/`id`/`kind`/`timestamp`). Every other member — and
  every stat type the server does not consume — is dropped whole. So
  `base64Certificate`, DTLS `fingerprint`, `usernameFragment`, candidate
  `address`/`ip`/`port`/`relatedAddress`/`url` and anything else cannot leak by
  omission. Retained strings are length-bounded. `src/webrtc.test.ts` seeds a
  report with each class of host-identifying member and asserts none survive
  `JSON.stringify(drain())`.
- **The trace-context carries no secrets** — `traceId`/`spanId` are random
  correlation handles only.

## W3C-correctness

A member that is **absent** on a source stat is **omitted** from the snapshot —
it is never coerced to `0`. The server engines depend on this (a missing counter
is _unknown_, not a measurement). `outputLatency` is surfaced but is a W3C
_estimate_; the server keeps that distinction (`baseLatency` is `measured`).

## Develop

```bash
pnpm --filter @earshot/browser build      # tsc -> dist (excludes tests + fakes)
pnpm --filter @earshot/browser typecheck  # tsc --noEmit (incl. tests)
pnpm --filter @earshot/browser test       # vitest run (mocked W3C APIs)
```
