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
  traceContext: { traceparent, traceId, spanId },   // W3C, random ids, no secrets
  snapshots:  [ { timestamp_ms, stats: { [id]: { ...RTCStats members } } } ],
  deviceEvents: [ { type, timestamp_ms, ...members } ],
}
```

`snapshots` feeds `analyze_webrtc_stats`; `deviceEvents` feeds
`analyze_audio_graph`. `src/roundtrip.test.ts` asserts the shapes line up with
those two functions' normalisers.

## Privacy posture — metadata only

- **No audio is ever read or retained.** The observed APIs expose counters,
  states and timings only; the kernel never touches audio samples.
- **No raw device identity leaves the client.** Device labels/ids and
  `AudioContext.sinkId` are replaced with opaque, **per-session salted** hashes
  (`dev_…` / `sink_…`). The salt is random per recorder, so hashes are not
  linkable across sessions and are not a stable fingerprint. Raw labels are
  never read into an event.
- **ICE candidate addresses are scrubbed.** IP/port/URL members are dropped from
  `getStats` output; only `networkType` and stat ids/states survive (exactly
  what the server engine reads).
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
