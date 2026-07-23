import { describe, expect, it } from "vitest";

import { createBrowserRecorder } from "./recorder.js";
import { normalizeStatsReport } from "./webrtc.js";
import {
  FakeClock,
  FakePeerConnection,
  FakeScheduler,
  makeStatsReport,
} from "./testing/fakes.js";

describe("normalizeStatsReport", () => {
  it("maps an RTCStatsReport into the { timestamp_ms, stats } server shape", () => {
    const report = makeStatsReport({
      "in-audio": {
        id: "in-audio",
        type: "inbound-rtp",
        kind: "audio",
        packetsReceived: 100,
        packetsLost: 0,
        jitter: 0.01,
        jitterBufferDelay: 1.5,
        jitterBufferEmittedCount: 1000,
      },
    });

    const snapshot = normalizeStatsReport(report, 4242);

    expect(snapshot.timestamp_ms).toBe(4242);
    const stat = snapshot.stats["in-audio"];
    expect(stat).toBeDefined();
    expect(stat).toMatchObject({
      type: "inbound-rtp",
      kind: "audio",
      packetsReceived: 100,
      packetsLost: 0,
      jitter: 0.01,
    });
  });

  it("omits a MISSING member entirely — never coerces it to 0", () => {
    const report = makeStatsReport({
      "in-audio": {
        id: "in-audio",
        type: "inbound-rtp",
        packetsReceived: 100,
        // no concealedSamples / totalSamplesReceived on this snapshot
      },
    });

    const stat = normalizeStatsReport(report, 1).stats["in-audio"];

    expect(stat).toBeDefined();
    expect("concealedSamples" in stat!).toBe(false);
    expect("totalSamplesReceived" in stat!).toBe(false);
    // The present member is intact and NOT defaulted.
    expect(stat!.packetsReceived).toBe(100);
  });

  it("scrubs raw ICE candidate network addresses but keeps networkType", () => {
    const report = makeStatsReport({
      "local-cand": {
        id: "local-cand",
        type: "local-candidate",
        networkType: "wifi",
        address: "192.168.1.44",
        ip: "192.168.1.44",
        port: 51423,
        url: "stun:stun.example.com:3478",
      },
    });

    const stat = normalizeStatsReport(report, 1).stats["local-cand"];

    expect(stat!.networkType).toBe("wifi");
    expect("address" in stat!).toBe(false);
    expect("ip" in stat!).toBe(false);
    expect("port" in stat!).toBe(false);
    expect("url" in stat!).toBe(false);
  });

  it("keys the snapshot by the stat's own id member when present", () => {
    const report = makeStatsReport({
      "map-key": { id: "member-id", type: "transport", iceState: "connected" },
    });

    const stats = normalizeStatsReport(report, 1).stats;

    expect(Object.keys(stats)).toEqual(["member-id"]);
  });
});

describe("attachPeerConnection interval sampling", () => {
  it("samples getStats() once per scheduled tick and buffers rising values", async () => {
    const scheduler = new FakeScheduler();
    const clock = new FakeClock(1000, 10);
    const pc = new FakePeerConnection([
      makeStatsReport({
        "in-audio": {
          id: "in-audio",
          type: "inbound-rtp",
          kind: "audio",
          packetsReceived: 100,
          packetsLost: 0,
          jitter: 0.01,
        },
      }),
      makeStatsReport({
        "in-audio": {
          id: "in-audio",
          type: "inbound-rtp",
          kind: "audio",
          packetsReceived: 200,
          packetsLost: 5,
          jitter: 0.03,
        },
      }),
      makeStatsReport({
        "in-audio": {
          id: "in-audio",
          type: "inbound-rtp",
          kind: "audio",
          packetsReceived: 300,
          packetsLost: 14,
          jitter: 0.06,
        },
      }),
    ]);

    const recorder = createBrowserRecorder({ scheduler, clock: clock.now });
    recorder.attachPeerConnection(pc, { intervalMs: 500 });

    await scheduler.fireAll(3);
    const { snapshots } = recorder.drain();

    expect(pc.getStatsCalls).toBe(3);
    expect(snapshots).toHaveLength(3);
    expect(snapshots.map((s) => s.stats["in-audio"]!.packetsLost)).toEqual([0, 5, 14]);
    expect(snapshots.map((s) => s.stats["in-audio"]!.jitter)).toEqual([0.01, 0.03, 0.06]);
    // Each snapshot carries a distinct, monotonic capture timestamp.
    expect(snapshots[0]!.timestamp_ms).toBeLessThan(snapshots[1]!.timestamp_ms);
    expect(snapshots[1]!.timestamp_ms).toBeLessThan(snapshots[2]!.timestamp_ms);
  });

  it("captures an ICE state transition across snapshots", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([
      makeStatsReport({
        transport: { id: "transport", type: "transport", iceState: "connected" },
      }),
      makeStatsReport({
        transport: { id: "transport", type: "transport", iceState: "disconnected" },
      }),
    ]);

    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);
    await scheduler.fireAll(2);
    const { snapshots } = recorder.drain();

    expect(snapshots[0]!.stats.transport!.iceState).toBe("connected");
    expect(snapshots[1]!.stats.transport!.iceState).toBe("disconnected");
  });

  it("fails open when getStats() rejects (no snapshot, no throw)", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([]); // empty -> getStats rejects
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);

    await expect(scheduler.fireAll(1)).resolves.toBeUndefined();
    expect(recorder.drain().snapshots).toHaveLength(0);
  });
});
