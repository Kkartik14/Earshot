/**
 * Installation self-check core (framework-free).
 *
 * Proves that `@earshot/browser`, as BUILT and consumed from the workspace, can
 * drive a recorder to a `CapturePayload` in the exact shape the two server
 * engines deserialise. It reuses the round-trip idea from
 * `packages/browser/src/roundtrip.test.ts`: mirror the Python normalisers'
 * preconditions in TS so a drift on either side fails here.
 *
 * Two deliberate import sources:
 *  - the recorder + protocol constants come from `@earshot/browser` — the
 *    resolved package `exports`, i.e. the BUILT `dist/` artifact. If the package
 *    cannot be built or imported, this file cannot even load.
 *  - the W3C fakes come from the SDK's own `src/testing/fakes.ts`. Those are
 *    excluded from the built package (they are test-only), so — exactly like the
 *    SDK's own tests — they are imported from source across the workspace.
 *
 * `assertConsumable()` throws an `Error` on the first mismatch and returns the
 * drained payload on success, so a caller (`selfcheck.test.ts`, or any runner)
 * can gate a release on it exiting non-zero.
 */

import {
  CAPTURE_PROTOCOL_VERSION,
  CONTINUOUS_CAPTURE_VERSION,
  createBrowserRecorder,
} from "@earshot/browser";
import type { CapturePayload, StatMembers } from "@earshot/browser";

import {
  FakeAudioContext,
  FakeClock,
  FakeMediaDevices,
  FakeMediaStream,
  FakeMediaTrack,
  FakePeerConnection,
  FakePermissionStatus,
  FakePermissions,
  FakeScheduler,
  makeStatsReport,
} from "../../../packages/browser/src/testing/fakes.js";

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/;

/** Mirror of the Python `_number`: a finite number that is not a boolean. */
function isEngineNumber(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value);
}

/** Mirror of the Python `_lower` precondition: a non-empty lower-case string. */
function isEngineType(value: unknown): boolean {
  return typeof value === "string" && value.length > 0 && value === value.toLowerCase();
}

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(`selfcheck: ${message}`);
}

/**
 * Drive a recorder from the built package against the SDK fakes and return the
 * drained payload. Uses `captureVersion: 2` (continuous capture), which is what
 * the example app uses, so the drainSequence path is exercised too.
 */
async function drivePayload(): Promise<CapturePayload> {
  const scheduler = new FakeScheduler();
  const driven = createBrowserRecorder({
    captureVersion: CONTINUOUS_CAPTURE_VERSION,
    scheduler,
    clock: new FakeClock(1000, 5).now,
  });

  const pc = new FakePeerConnection([
    makeStatsReport({
      "in-audio": {
        id: "in-audio",
        type: "inbound-rtp",
        kind: "audio",
        packetsReceived: 500,
        packetsLost: 2,
        jitter: 0.012,
        jitterBufferDelay: 4.2,
        jitterBufferEmittedCount: 40000,
        concealedSamples: 120,
        totalSamplesReceived: 480000,
      },
      transport: {
        id: "transport",
        type: "transport",
        iceState: "connected",
        selectedCandidatePairId: "pair",
      },
      local: { id: "local", type: "local-candidate", networkType: "wifi" },
    }),
  ]);
  driven.attachPeerConnection(pc);
  await scheduler.fireAll(1);

  // Real AudioContext render path (state + latency + sample rate), then the
  // getUserMedia grant path with a real (fake) track carrying a device id.
  const ctx = new FakeAudioContext({
    state: "running",
    baseLatency: 0.005,
    outputLatency: 0.02,
    sampleRate: 48000,
  });
  driven.attachAudioContext(ctx);
  ctx.setState("suspended");

  const devices = new FakeMediaDevices({
    stream: new FakeMediaStream([
      new FakeMediaTrack({ deviceId: "mic-1", sampleRate: 44100 }),
    ]),
  });
  await driven.observeMediaDevices(devices, {
    permissions: new FakePermissions(new FakePermissionStatus("granted")),
  });
  await driven.requestMicrophone(devices); // granted + device hash + sample-rate check

  return driven.drain();
}

/** Run every assertion; throw on the first mismatch; return the payload. */
export async function assertConsumable(): Promise<CapturePayload> {
  assert(CAPTURE_PROTOCOL_VERSION === 1, "CAPTURE_PROTOCOL_VERSION must be 1");
  assert(CONTINUOUS_CAPTURE_VERSION === 2, "CONTINUOUS_CAPTURE_VERSION must be 2");

  const payload = await drivePayload();

  // -- envelope --------------------------------------------------------------
  assert(
    payload.captureVersion === CONTINUOUS_CAPTURE_VERSION,
    `captureVersion should be ${CONTINUOUS_CAPTURE_VERSION}, got ${payload.captureVersion}`,
  );
  assert(Number.isInteger(payload.captureVersion), "captureVersion must be an integer");
  assert(/^sess_/.test(payload.sessionId), "sessionId must start with sess_");
  assert(
    TRACEPARENT.test(payload.traceContext.traceparent),
    "traceContext.traceparent must be a valid W3C traceparent",
  );
  assert(
    payload.clockDomain.kind === "browser_monotonic" && payload.clockDomain.unit === "ms",
    "clockDomain must be a browser_monotonic ms domain",
  );
  // captureVersion 2 continuous-capture fields:
  assert(
    payload.drainSequence === 1,
    `first drain must be sequence 1, got ${payload.drainSequence}`,
  );
  assert(
    isEngineNumber(payload.capturerStartedAtMs),
    "capturerStartedAtMs must be a finite number under captureVersion 2",
  );

  // -- snapshots -> analyze_webrtc_stats ------------------------------------
  assert(payload.snapshots.length > 0, "expected at least one getStats snapshot");
  for (const snapshot of payload.snapshots) {
    assert(
      isEngineNumber(snapshot.timestamp_ms),
      "snapshot.timestamp_ms must be a finite number",
    );
    assert(typeof snapshot.stats === "object", "snapshot.stats must be an object");
    for (const [id, stat] of Object.entries(snapshot.stats)) {
      assert(
        typeof id === "string" && id.length > 0,
        "stat id must be a non-empty string",
      );
      assert(stat !== null && typeof stat === "object", "stat value must be an object");
    }
  }
  const inbound: StatMembers | undefined = payload.snapshots[0]?.stats["in-audio"];
  assert(inbound !== undefined, "expected the in-audio inbound-rtp stat");
  assert(inbound?.type === "inbound-rtp", "in-audio.type must be inbound-rtp");
  assert(isEngineNumber(inbound?.packetsReceived), "packetsReceived must be a number");
  assert(isEngineNumber(inbound?.packetsLost), "packetsLost must be a number");
  assert(
    isEngineNumber(inbound?.jitterBufferEmittedCount),
    "jitterBufferEmittedCount must be a number",
  );
  assert(isEngineNumber(inbound?.concealedSamples), "concealedSamples must be a number");
  // A member that was ABSENT on the source stat must stay absent (never 0).
  assert(
    inbound?.packetsDiscarded === undefined,
    "an unset stat member must stay missing, never coerced to 0",
  );
  assert(
    payload.snapshots[0]?.stats.transport?.iceState === "connected",
    "transport.iceState must survive",
  );
  assert(
    payload.snapshots[0]?.stats.local?.networkType === "wifi",
    "local-candidate.networkType must survive",
  );

  // -- deviceEvents -> analyze_audio_graph ----------------------------------
  assert(payload.deviceEvents.length > 0, "expected at least one device event");
  for (const event of payload.deviceEvents) {
    assert(
      isEngineType(event.type),
      `device event type '${String(event.type)}' must be a non-empty lower-case string`,
    );
    assert(
      isEngineNumber(event.timestamp_ms),
      "device event timestamp_ms must be a finite number",
    );
  }
  const types = payload.deviceEvents.map((event) => event.type);
  for (const required of [
    "latency",
    "audiocontext_state",
    "permission",
    "sample_rate_mismatch",
  ]) {
    assert(
      types.includes(required),
      `expected a '${required}' device event; got ${types.join(", ")}`,
    );
  }
  const permission = payload.deviceEvents.find((event) => event.type === "permission");
  assert(
    permission?.state === "granted",
    "the getUserMedia grant must record a granted permission",
  );
  const mismatch = payload.deviceEvents.find(
    (event) => event.type === "sample_rate_mismatch",
  );
  assert(
    isEngineNumber(mismatch?.configured_hz) && isEngineNumber(mismatch?.actual_hz),
    "sample_rate_mismatch must carry numeric configured_hz/actual_hz",
  );

  // -- coverage --------------------------------------------------------------
  assert(Array.isArray(payload.coverage), "coverage must be an array");
  for (const note of payload.coverage) {
    assert(
      typeof note.signal === "string" && note.signal.length > 0,
      "each coverage note needs a non-empty signal",
    );
    assert(
      typeof note.availability === "string",
      "coverage.availability must be a string",
    );
    assert(typeof note.reason === "string", "coverage.reason must be a string");
  }

  // -- wire-serialisable (the server receives JSON) --------------------------
  const roundTripped = JSON.parse(JSON.stringify(payload)) as CapturePayload;
  assert(
    roundTripped.captureVersion === payload.captureVersion &&
      roundTripped.snapshots.length === payload.snapshots.length &&
      roundTripped.deviceEvents.length === payload.deviceEvents.length,
    "payload must survive JSON serialisation unchanged in shape",
  );

  return payload;
}
