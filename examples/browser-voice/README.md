# @earshot/example-browser-voice

A **real, runnable browser voice app** that captures a live
`getUserMedia` → `RTCPeerConnection` → `AudioContext` session with
[`@earshot/browser`](../../packages/browser) and POSTs it to the earshot
backend's `POST /v1/capture`. It is a **private workspace example** — it consumes
the SDK through the pnpm workspace, not from npm, and is never published.

Its purpose is to make the code paths the SDK's single Chrome loopback run left
unexercised **exist and be runnable**: the real microphone-permission path, sink
switching, and `outputLatency` — across whatever browser you point at it.

## What it exercises (real code paths, no fabricated metrics)

Both modes drive the SDK against **real** browser objects. Where a browser does
not expose a signal, the app relies on the SDK's own coverage-note behaviour and
does **not** invent a value.

| Path                                       | How this app drives it                                                                                                                                             |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Microphone permission** (`getUserMedia`) | `session.requestMicrophone(navigator.mediaDevices, { audio: … })` → the SDK records a real `granted`/`denied` event                                                |
| **Permissions API** (`microphone`)         | `session.observeMediaDevices(navigator.mediaDevices, { permissions: navigator.permissions })`                                                                      |
| **`RTCPeerConnection.getStats()`**         | a real peer connection is sampled every 1s (`attachPeerConnection`)                                                                                                |
| **`AudioContext` render path**             | the received stream is connected to the context's output, so `state`, `outputLatency`, and the `getOutputTimestamp()` render-queue are real (`attachAudioContext`) |
| **Sink switching** (`setSinkId`)           | a device dropdown calls `AudioContext.setSinkId`, whose `sinkchange` the SDK captures — **feature-detected**, see below                                            |
| **Delivery**                               | drained batches are POSTed to `POST /v1/capture` via the SDK transport, with the recorder wired as the coverage sink                                               |

It uses **`captureVersion: 2`** (continuous capture) — the version the SDK
exposes as `CONTINUOUS_CAPTURE_VERSION` — so each drain carries a monotonic
`drainSequence` under one stable session and the server accumulates the whole
call.

### Two modes

- **Local WebRTC loopback** (`src/loopback.ts`) — needs **no** external service
  and **no** key. Your real microphone is piped through two real
  `RTCPeerConnection`s (sender → receiver) and rendered through a real
  `AudioContext`. The receiver's `getStats()` carries the `inbound-rtp` audio
  stats the server engine reads. Received audio is routed through a gain node
  defaulting to **0 (silent)** so it will not howl next to your mic; the render
  path is still live, so `outputLatency`/`getOutputTimestamp()` are real. Raise
  the monitor slider (headphones advised) to hear it.

- **Raw OpenAI Realtime** (`src/openai-realtime.ts`) — earshot capture wrapped
  around a **raw** OpenAI Realtime **WebRTC** voice connection (no OpenAI SDK):
  it adds the mic track to an `RTCPeerConnection`, opens the `oai-events` data
  channel, POSTs the SDP offer to `https://api.openai.com/v1/realtime?model=…`
  with `Authorization: Bearer <runtime key>` + `Content-Type: application/sdp`,
  and applies the SDP answer. WebRTC is the correct browser transport; OpenAI's
  WebSocket transport is for server-side use (a browser cannot set the
  `Authorization` header on a WebSocket handshake).

## The OpenAI key is read at runtime — never committed, never needed to build

The Realtime key is read **only** from the runtime UI field. It is not read from
build-time env, not hardcoded, and not committed, so the example **builds and
typechecks with no key present** (the field is simply empty and connecting
without one throws a clear error before any network call). Use an **ephemeral
client secret** minted server-side with `POST /v1/realtime/sessions` rather than
a standing API key.

**No automated test in this package ever calls the OpenAI API.** Only the pure
URL/credential helpers in `src/realtime-url.ts` are unit-tested; the connection
runs solely in the browser at runtime.

## Installation self-check (`pnpm run selfcheck`)

`selfcheck/` verifies that `@earshot/browser` can actually be **consumed** as a
built package:

```bash
pnpm --filter @earshot/example-browser-voice run selfcheck
```

It **builds** `@earshot/browser`, **imports** it (the resolved package
`exports`, i.e. the built `dist/`), **drives a recorder against the SDK's own
test fakes**, and **asserts `drain()` returns a `CapturePayload` of the shape the
server engines expect** — reusing the round-trip idea from
`packages/browser/src/roundtrip.test.ts`. It asserts, among other things:

- the envelope: `captureVersion === 2`, integer; `sessionId` (`sess_…`); a valid
  W3C `traceparent`; a `browser_monotonic`/`ms` clock domain; a `drainSequence`
  of 1 and a finite `capturerStartedAtMs`;
- `snapshots` satisfy `analyze_webrtc_stats`' normaliser (finite `timestamp_ms`,
  object stats, `inbound-rtp` members numeric, `iceState`/`networkType` survive)
  and **a member absent on the source stat stays absent — never coerced to `0`**;
- `deviceEvents` satisfy `analyze_audio_graph`' normaliser (non-empty lower-case
  `type`, finite `timestamp_ms`) and include `latency`, `audiocontext_state`,
  `permission` (`granted`), `sample_rate_mismatch`;
- `coverage` is well-formed and the whole payload survives JSON serialisation.

**It exits non-zero on any mismatch**, so it can gate a release: if the SDK
cannot be built or imported, or if `drain()` ever drifts from the shape the
server consumes, the command fails.

## What is validated — and what is not

**Be precise about this.** This example makes the microphone, sink-switching,
Safari, Firefox, and output-latency paths _exist and runnable_; it does not
prove them on real hardware.

**Validated in this repo (automated):**

- The example **typechecks** (`tsc -b`), **builds** (`vite build`), its unit
  tests pass, and the **installation self-check passes** — proving
  `@earshot/browser` is consumable from the workspace and that a driven `drain()`
  yields a server-shaped `CapturePayload` (driven against the SDK fakes; **no
  browser is involved** in the self-check).

**NOT validated here (paths made to exist and run, but not exercised):**

- **Real hardware.** This app has **not** been run in any browser in this repo.
  Its browser code (`main.ts`, `loopback.ts`, `openai-realtime.ts`) is
  typechecked and bundled, not executed here.
- **Safari and Firefox** — no run at all; and Firefox/older Safari lack
  `AudioContext.setSinkId`, so sink switching is disabled there (the SDK then
  records no `sink_change`, which is the honest signal).
- **The real `getUserMedia` microphone + Permissions API** on real devices.
- **`setSinkId`/`sinkchange` and `outputLatency`/`getOutputTimestamp()`** with
  real audio flowing.
- **The OpenAI Realtime connection** — no key is used and no OpenAI call is made
  in this repo.
- **A live browser → `POST /v1/capture` round-trip.** The SDK's delivery logic is
  unit-tested inside the SDK; this app wires it, but an end-to-end browser
  delivery was not run here.

The only on-device confirmation that exists for `@earshot/browser` is the single
**Chrome loopback** pass documented in
[`packages/browser/README.md`](../../packages/browser/README.md), which used a
**synthesised** audio source (so no microphone permission) — exactly the gap this
app is built to let you close on your own hardware.

## Run it

From the repo root:

```bash
pnpm install
pnpm --filter @earshot/example-browser-voice dev
```

Then open the printed URL. For **loopback** mode you need nothing else. Local
delivery uses the Vite proxy from `/v1` to `http://127.0.0.1:8000` (overridable
with `EARSHOT_API_URL`); start that backend with `pnpm serve`. For a hosted
deployment, point the endpoint field at the host application's authenticated
BFF route and provide its CSRF token if required. The form's project id is an
assertion; the BFF must derive and verify project scope from its session. The
opaque auth-context field is a local demo value. A hosted integration must
replace it with the host's dynamic, nonsecret `getAuthContextId()` value, which
changes whenever the effective user/project grant changes. It must not contain a
raw user id or credential. The browser example has no Earshot project API key
option; the host BFF supplies Earshot service credentials on the server.
Loopback still runs and captures without a backend — the SDK records
undelivered batches as coverage.

The Stop button closes the voice mode first, then calls `CaptureSession.endCall()`
to send the v2 finality marker. The result reports the final delivery outcome,
acceptance-observer failures, and any exact retry still retained after the
bounded flush. The example shows an error if the final drain is unaccepted or an
observer failed. `CaptureSession.stop()` is observer-only and does not claim that
a call ended. A hosted application must persist finalized `call_id` values in
its durable seal outbox and reconcile ambiguous seal responses as described in
[`platform-integration.md`](../../docs/platform-integration.md). This sample
does not implement that host-owned outbox.

If the final batch receives `413`, the example keeps its transport available and
offers **Drop snapshots/events; send final marker**. That sends a smaller replacement at
the same session and drain sequence, preserving the clock and recorded end reason
while declaring omitted snapshots/events in `capture.upload` coverage. This is
an explicit evidence-loss recovery. The action appears only when the final batch
contains observations that can be removed to make a smaller body. If no smaller
body can be made, or the reduced marker also receives `413`, the example reports
host repair required; the server may have exhausted its cumulative journal
capacity or another configured capture limit.

Other scripts:

```bash
pnpm --filter @earshot/example-browser-voice typecheck   # tsc -b
pnpm --filter @earshot/example-browser-voice build       # tsc -b && vite build
pnpm --filter @earshot/example-browser-voice test        # vitest (pure helpers)
pnpm --filter @earshot/example-browser-voice run selfcheck
```

## Privacy

The Earshot capture upload is metadata only: `@earshot/browser` reads browser
statistics, states, and timings, not audio samples, and does not send audio,
transcripts, or tool payloads to Earshot. It replaces device and sink ids with
opaque per-session salted hashes before telemetry leaves the client.

Audio routing depends on the selected mode. **Local WebRTC loopback** keeps
microphone audio within the browser. **OpenAI Realtime** sends the microphone
stream and SDP offer to OpenAI for processing; that provider audio is separate
from the metadata sent to Earshot. Output-device **labels** shown in the
dropdown are read locally for the UI and are never sent to Earshot. See the
SDK's telemetry privacy posture in `packages/browser/README.md`.

```

```
