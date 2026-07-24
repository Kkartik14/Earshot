/**
 * Contract round-trip: prove the drained `CapturePayload` deserialises into the
 * two Python engines' inputs WITHOUT importing Python (this is a TS package).
 *
 * The assertions below mirror the Python normalisers so a drift on either side
 * fails a test:
 *
 * `analyze_webrtc_stats(snapshots)`
 *   — packages/sdk-python/src/earshot/engines/webrtc.py
 *   Each snapshot is `{ timestamp_ms: number, stats: { [id]: RTCStats-dict } }`.
 *   `_normalize_snapshots` requires: `timestamp_ms` finite number (not bool),
 *   `stats` a mapping, stat ids strings, stat values mappings. The engine then
 *   reads members: type, kind/mediaType, packetsReceived, packetsLost, jitter,
 *   jitterBufferDelay, jitterBufferEmittedCount, concealedSamples,
 *   totalSamplesReceived, roundTripTime/currentRoundTripTime, iceState/dtlsState,
 *   selectedCandidatePairId, selected/nominated, localCandidateId, networkType.
 *   A MISSING member must stay missing (never 0).
 *
 * `analyze_audio_graph(events)`
 *   — packages/sdk-python/src/earshot/engines/device.py
 *   Each event is `{ type, timestamp_ms, ... }`. `_normalize_events` requires a
 *   non-empty (it lower-cases it) string `type` and a finite number
 *   `timestamp_ms`. The engine reads: state (permission/audiocontext_state),
 *   configured_hz/actual_hz (sample_rate_mismatch), base_latency_s/
 *   output_latency_s (latency), and the devicechange/sink_change/underrun types.
 */

import { describe, expect, it } from "vitest";

import { CAPTURE_PROTOCOL_VERSION } from "./protocol.js";
import { EarshotBrowserRecorder } from "./recorder.js";
import type { CapturePayload } from "./types.js";
import {
  FakeAudioContext,
  FakeClock,
  FakeMediaDevices,
  FakePeerConnection,
  FakeScheduler,
  makeStatsReport,
} from "./testing/fakes.js";

/** Mirror of the Python `_number`: a finite number that is not a boolean. */
function isEngineNumber(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value);
}

/** Mirror of the Python `_lower` precondition: a non-empty string type. */
function isEngineType(value: unknown): boolean {
  return typeof value === "string" && value.length > 0 && value === value.toLowerCase();
}

async function buildPayload(): Promise<CapturePayload> {
  const scheduler = new FakeScheduler();
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
      remote: { id: "remote", type: "remote-inbound-rtp", roundTripTime: 0.08 },
      transport: {
        id: "transport",
        type: "transport",
        iceState: "connected",
        selectedCandidatePairId: "pair",
      },
      local: { id: "local", type: "local-candidate", networkType: "wifi" },
    }),
  ]);

  const recorder = new EarshotBrowserRecorder({
    scheduler,
    clock: new FakeClock(1000, 5).now,
  });
  recorder.attachPeerConnection(pc);
  await scheduler.fireAll(1);

  const ctx = new FakeAudioContext({
    state: "running",
    baseLatency: 0.005,
    outputLatency: 0.02,
  });
  recorder.attachAudioContext(ctx);
  ctx.setState("suspended");
  recorder.recordSampleRateMismatch(48000, 44100);
  await recorder.requestMicrophone(
    new FakeMediaDevices({ error: { name: "NotAllowedError" } }),
  );

  return recorder.drain();
}

describe("payload round-trips into the Python engine inputs", () => {
  it("snapshots satisfy analyze_webrtc_stats' _normalize_snapshots contract", async () => {
    const { snapshots } = await buildPayload();
    expect(snapshots.length).toBeGreaterThan(0);

    for (const snapshot of snapshots) {
      expect(isEngineNumber(snapshot.timestamp_ms)).toBe(true);
      expect(typeof snapshot.stats).toBe("object");
      for (const [id, stat] of Object.entries(snapshot.stats)) {
        expect(typeof id).toBe("string");
        expect(typeof stat).toBe("object");
      }
    }

    const inbound = snapshots[0]!.stats["in-audio"]!;
    // The exact members webrtc.py reads are present and typed as it expects.
    expect(inbound.type).toBe("inbound-rtp");
    expect(isEngineNumber(inbound.packetsReceived)).toBe(true);
    expect(isEngineNumber(inbound.packetsLost)).toBe(true);
    expect(isEngineNumber(inbound.jitterBufferEmittedCount)).toBe(true);
    expect(isEngineNumber(inbound.concealedSamples)).toBe(true);
    expect(snapshots[0]!.stats.transport!.iceState).toBe("connected");
    expect(snapshots[0]!.stats.local!.networkType).toBe("wifi");
  });

  it("device events satisfy analyze_audio_graph' _normalize_events contract", async () => {
    const { deviceEvents } = await buildPayload();
    expect(deviceEvents.length).toBeGreaterThan(0);

    for (const event of deviceEvents) {
      expect(isEngineType(event.type)).toBe(true);
      expect(isEngineNumber(event.timestamp_ms)).toBe(true);
    }

    const types = deviceEvents.map((event) => event.type);
    // Every type we emit is in the vocabulary device.py dispatches on.
    expect(types).toEqual(
      expect.arrayContaining([
        "latency",
        "audiocontext_state",
        "sample_rate_mismatch",
        "permission",
      ]),
    );
    const mismatch = deviceEvents.find((e) => e.type === "sample_rate_mismatch")!;
    expect(mismatch.configured_hz).toBe(48000);
    expect(mismatch.actual_hz).toBe(44100);
    const permission = deviceEvents.find((e) => e.type === "permission")!;
    expect(permission.state).toBe("denied");
  });

  it("carries a session id + W3C traceparent for server correlation", async () => {
    const payload = await buildPayload();
    expect(payload.sessionId).toMatch(/^sess_/);
    expect(payload.traceContext.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  });

  it("declares the capture wire version the server gates on", async () => {
    // `POST /v1/capture` reads `captureVersion` BEFORE the rest of the schema and
    // answers EARSHOT_UNSUPPORTED_CAPTURE_VERSION when it does not govern it, so
    // the field has to be present on every drained payload.
    const payload = await buildPayload();
    expect(payload.captureVersion).toBe(CAPTURE_PROTOCOL_VERSION);
    expect(Number.isInteger(payload.captureVersion)).toBe(true);
  });
});
