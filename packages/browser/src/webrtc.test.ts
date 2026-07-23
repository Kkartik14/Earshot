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

  it("privacy sentinel: certificates, fingerprints, usernameFragment, addresses and labels never survive", async () => {
    // A report seeded with every class of host-identifying member a real
    // getStats() can expose, alongside the governed metrics we DO consume.
    const report = makeStatsReport({
      cert: {
        id: "cert",
        type: "certificate",
        base64Certificate: "MIIB-fake-cert-material-AAAA",
        fingerprint: "AA:BB:CC:DD:EE:FF",
        fingerprintAlgorithm: "sha-256",
      },
      "in-audio": {
        id: "in-audio",
        type: "inbound-rtp",
        kind: "audio",
        packetsReceived: 100,
        packetsLost: 1,
        jitter: 0.02,
        // A stray sensitive member on a stat we DO keep must still be dropped.
        trackIdentifier: "Jabra Elite 75t",
      },
      pair: {
        id: "pair",
        type: "candidate-pair",
        selected: true,
        state: "succeeded",
        currentRoundTripTime: 0.04,
        usernameFragment: "s3cr3tUfrag",
      },
      local: {
        id: "local",
        type: "local-candidate",
        networkType: "wifi",
        address: "203.0.113.7",
        ip: "203.0.113.7",
        port: 51999,
        relatedAddress: "10.0.0.5",
        url: "stun:stun.example.com:3478",
        usernameFragment: "s3cr3tUfrag",
        candidateType: "srflx",
      },
    });

    const snapshot = normalizeStatsReport(report, 4242);
    const serialized = JSON.stringify(snapshot);

    // NONE of the host-identifying material survives serialisation.
    expect(serialized).not.toContain("base64Certificate");
    expect(serialized).not.toContain("fake-cert-material");
    expect(serialized).not.toContain("fingerprint");
    expect(serialized).not.toContain("usernameFragment");
    expect(serialized).not.toContain("s3cr3tUfrag");
    expect(serialized).not.toContain("203.0.113.7");
    expect(serialized).not.toContain("10.0.0.5");
    expect(serialized).not.toContain("stun.example.com");
    expect(serialized).not.toContain("Jabra");
    expect(serialized).not.toContain("candidateType");
    // The whole unconsumed certificate stat is gone.
    expect(snapshot.stats.cert).toBeUndefined();
    // ...but the safe governed telemetry the engine reads is intact.
    expect(serialized).toContain("wifi");
    expect(snapshot.stats["in-audio"]!.jitter).toBe(0.02);
    expect(snapshot.stats["in-audio"]!.packetsReceived).toBe(100);
    expect(snapshot.stats.local!.networkType).toBe("wifi");
    expect(snapshot.stats.pair!.currentRoundTripTime).toBe(0.04);
    expect(snapshot.stats.pair!.selected).toBe(true);
  });

  it("bounds retained string members to a defensive length cap", () => {
    const report = makeStatsReport({
      t: { id: "t", type: "transport", iceState: "x".repeat(5000) },
    });
    const stat = normalizeStatsReport(report, 1).stats.t;
    expect((stat!.iceState as string).length).toBeLessThanOrEqual(128);
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
