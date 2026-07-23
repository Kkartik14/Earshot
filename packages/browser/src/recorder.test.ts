import { describe, expect, it } from "vitest";

import { createBrowserRecorder, EarshotBrowserRecorder } from "./recorder.js";
import type { PermissionsLike } from "./types.js";
import {
  ControllablePeerConnection,
  FakeAudioContext,
  FakeClock,
  FakeMediaDevices,
  FakeMediaStream,
  FakeMediaTrack,
  FakePeerConnection,
  FakeScheduler,
  makeStatsReport,
  sequentialRandom,
} from "./testing/fakes.js";

describe("session identity + trace context", () => {
  it("mints a sess_ id and a stable trace context, honoured across drains", () => {
    const recorder = createBrowserRecorder({ random: sequentialRandom(3) });

    expect(recorder.sessionId).toMatch(/^sess_[0-9a-f]{16}$/);
    const first = recorder.drain();
    const second = recorder.drain();
    expect(first.sessionId).toBe(recorder.sessionId);
    expect(first.traceContext).toBe(recorder.traceContext());
    expect(second.traceContext.traceparent).toBe(first.traceContext.traceparent);
  });

  it("honours an explicit sessionId and injects the traceparent header", () => {
    const recorder = createBrowserRecorder({ sessionId: "sess_custom" });
    expect(recorder.sessionId).toBe("sess_custom");
    expect(recorder.injectTraceHeaders().traceparent).toBe(
      recorder.traceContext().traceparent,
    );
  });
});

describe("drain", () => {
  it("returns the buffered payload and resets the buffers", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([
      makeStatsReport({ t: { id: "t", type: "transport", iceState: "connected" } }),
    ]);
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);
    await scheduler.fireAll(1);

    expect(recorder.drain().snapshots).toHaveLength(1);
    expect(recorder.drain().snapshots).toHaveLength(0); // drained -> empty
  });
});

describe("stop", () => {
  it("clears intervals + listeners and makes further capture inert", async () => {
    const scheduler = new FakeScheduler();
    const ctx = new FakeAudioContext({ state: "running" });
    const pc = new FakePeerConnection([
      makeStatsReport({ t: { id: "t", type: "transport", iceState: "connected" } }),
    ]);
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);
    recorder.attachAudioContext(ctx);

    expect(scheduler.activeCount).toBe(1);
    expect(ctx.listenerCount()).toBe(2); // statechange + sinkchange

    recorder.stop();

    expect(scheduler.activeCount).toBe(0);
    expect(ctx.listenerCount()).toBe(0);

    // Nothing fires or records after stop().
    await scheduler.fireAll(3);
    ctx.setState("suspended");
    expect(pc.getStatsCalls).toBe(0);
    expect(recorder.drain().snapshots).toHaveLength(0);
    expect(recorder.drain().deviceEvents).toHaveLength(0);
  });

  it("is idempotent", () => {
    const recorder = createBrowserRecorder();
    recorder.stop();
    expect(() => recorder.stop()).not.toThrow();
  });
});

describe("bounded buffers + explicit loss reporting (F8)", () => {
  const inbound = (packetsReceived: number) =>
    makeStatsReport({
      in: { id: "in", type: "inbound-rtp", kind: "audio", packetsReceived },
    });

  it("bounds the snapshot buffer, dropping the OLDEST and recording the loss", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([inbound(0), inbound(1), inbound(2)]);
    const recorder = createBrowserRecorder({
      scheduler,
      maxSnapshots: 2,
      clock: new FakeClock().now,
    });
    recorder.attachPeerConnection(pc, { intervalMs: 100 });

    await scheduler.fireAll(3);
    const payload = recorder.drain();

    // Only the most-recent window is kept; the oldest (0) was dropped.
    expect(payload.snapshots).toHaveLength(2);
    expect(payload.snapshots.map((s) => s.stats.in!.packetsReceived)).toEqual([1, 2]);
    const cov = payload.coverage.find((c) => c.signal === "webrtc.snapshots");
    expect(cov).toBeDefined();
    expect(cov!.availability).toBe("partial");
    expect(cov!.reason).toBe("buffer_overflow_oldest_dropped");
    expect(cov!.droppedCount).toBe(1);
  });

  it("bounds the device-event buffer and records the loss", () => {
    const recorder = createBrowserRecorder({ maxDeviceEvents: 2 });
    recorder.recordRenderGlitch("underrun");
    recorder.recordRenderGlitch("glitch");
    recorder.recordRenderGlitch("dropped_frames");

    const payload = recorder.drain();
    expect(payload.deviceEvents).toHaveLength(2);
    const cov = payload.coverage.find((c) => c.signal === "device.events");
    expect(cov).toBeDefined();
    expect(cov!.droppedCount).toBe(1);
  });

  it("rejects an invalid polling interval with a clear error", () => {
    const recorder = createBrowserRecorder({ scheduler: new FakeScheduler() });
    const pc = new FakePeerConnection([]);
    expect(() => recorder.attachPeerConnection(pc, { intervalMs: 0 })).toThrow(
      /positive finite/,
    );
    expect(() => recorder.attachPeerConnection(pc, { intervalMs: -5 })).toThrow();
    expect(() => recorder.attachPeerConnection(pc, { intervalMs: Number.NaN })).toThrow();
    expect(() =>
      recorder.attachPeerConnection(pc, { intervalMs: Number.POSITIVE_INFINITY }),
    ).toThrow();
  });

  it("skips an OVERLAPPING getStats() and records it, never running two at once", async () => {
    const scheduler = new FakeScheduler();
    const pc = new ControllablePeerConnection(
      makeStatsReport({ t: { id: "t", type: "transport", iceState: "connected" } }),
    );
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc, { intervalMs: 100 });

    const [tick] = scheduler.handlers();
    const first = tick!() as Promise<void>; // getStats #1 starts and stays in flight
    const second = tick!() as Promise<void>; // in flight -> skipped, not a 2nd getStats

    expect(pc.getStatsCalls).toBe(1);
    expect(pc.pending).toBe(1);

    pc.resolveNext();
    await Promise.all([first, second]);

    const payload = recorder.drain();
    expect(payload.snapshots).toHaveLength(1);
    const overlap = payload.coverage.find((c) => c.signal === "webrtc.getstats_overlap");
    expect(overlap).toBeDefined();
    expect(overlap!.reason).toBe("overlapping_sample_skipped");
    expect(overlap!.droppedCount).toBe(1);
  });

  it("records a getStats() rejection as explicit coverage, not silence", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([]); // getStats rejects
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);

    await scheduler.fireAll(1);
    const payload = recorder.drain();

    expect(payload.snapshots).toHaveLength(0);
    const cov = payload.coverage.find((c) => c.signal === "webrtc.getstats");
    expect(cov).toBeDefined();
    expect(cov!.reason).toBe("getstats_failed");
    expect(cov!.droppedCount).toBe(1);
  });

  it("records a rejected microphone permission query as coverage", async () => {
    const mediaDevices = new FakeMediaDevices();
    const permissions: PermissionsLike = {
      query: () => Promise.reject(new Error("unsupported descriptor")),
    };
    const recorder = createBrowserRecorder();

    await recorder.observeMediaDevices(mediaDevices, { permissions });
    const cov = recorder.drain().coverage.find((c) => c.signal === "device.permission_query");

    expect(cov).toBeDefined();
    expect(cov!.availability).toBe("not_observed");
    expect(cov!.reason).toBe("permission_query_failed");
  });

  it("resets per-window coverage counters after a drain", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([]); // rejects -> a coverage gap
    const recorder = createBrowserRecorder({ scheduler });
    recorder.attachPeerConnection(pc);

    await scheduler.fireAll(1);
    expect(recorder.drain().coverage).toHaveLength(1);
    // The next window starts clean.
    expect(recorder.drain().coverage).toHaveLength(0);
  });
});

describe("trace-context join (F8)", () => {
  const APP_TRACEPARENT = "00-1234567890abcdef1234567890abcdef-1122334455667788-01";

  it("JOINS an application-supplied traceparent instead of minting a new one", () => {
    const recorder = createBrowserRecorder({ traceparent: APP_TRACEPARENT });
    expect(recorder.traceContext().traceparent).toBe(APP_TRACEPARENT);
    expect(recorder.traceContext().traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(recorder.traceContext().spanId).toBe("1122334455667788");
    expect(recorder.drain().traceContext.traceparent).toBe(APP_TRACEPARENT);
  });

  it("accepts a full TraceContext option verbatim", () => {
    const ctx = {
      traceparent: APP_TRACEPARENT,
      traceId: "1234567890abcdef1234567890abcdef",
      spanId: "1122334455667788",
    };
    const recorder = createBrowserRecorder({ traceContext: ctx });
    expect(recorder.traceContext()).toBe(ctx);
  });

  it("mints a fresh context when no trace is supplied", () => {
    const recorder = createBrowserRecorder({ random: sequentialRandom(5) });
    expect(recorder.traceContext().traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  });

  it("mints a fresh context when the supplied traceparent is not spec-valid", () => {
    const recorder = createBrowserRecorder({ traceparent: "garbage-not-a-traceparent" });
    expect(recorder.traceContext().traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  });

  it("never overwrites a traceparent already present on the outgoing headers", () => {
    const recorder = createBrowserRecorder();
    const headers = recorder.injectTraceHeaders({ traceparent: APP_TRACEPARENT });
    expect(headers.traceparent).toBe(APP_TRACEPARENT);
  });
});

describe("browser clock-domain identity (F2)", () => {
  it("stamps a stable clock-domain id, carried unchanged across drains", async () => {
    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([
      makeStatsReport({ t: { id: "t", type: "transport", iceState: "connected" } }),
    ]);
    const recorder = createBrowserRecorder({ scheduler, clock: new FakeClock(5000, 5).now });
    recorder.attachPeerConnection(pc);
    await scheduler.fireAll(1);

    const first = recorder.drain();
    const second = recorder.drain();
    expect(first.clockDomain.id).toMatch(/^clk_[0-9a-f]{16}$/);
    expect(first.clockDomain.kind).toBe("browser_monotonic");
    expect(first.clockDomain.unit).toBe("ms");
    expect(first.clockDomain.uncertaintyMs).toBeGreaterThanOrEqual(0);
    // The clock-domain id is stable across drains (one continuous browser timeline).
    expect(second.clockDomain.id).toBe(first.clockDomain.id);
    // timestamps are RAW monotonic readings, not rebased to zero.
    expect(first.snapshots[0]!.timestamp_ms).toBeGreaterThanOrEqual(5000);
  });
});

describe("privacy: no raw device identity leaves the client", () => {
  it("keeps device labels/ids and ICE addresses out of the drained payload", async () => {
    const label = "Jabra Elite 75t";
    const deviceId = "raw-device-id-9f8a7b";
    const track = new FakeMediaTrack({ deviceId, label });
    const mediaDevices = new FakeMediaDevices({ stream: new FakeMediaStream([track]) });

    const scheduler = new FakeScheduler();
    const pc = new FakePeerConnection([
      makeStatsReport({
        pair: {
          id: "pair",
          type: "candidate-pair",
          selected: true,
          state: "succeeded",
          address: "203.0.113.7",
          port: 51999,
        },
        local: {
          id: "local",
          type: "local-candidate",
          networkType: "wifi",
          ip: "203.0.113.7",
        },
      }),
    ]);

    const recorder = new EarshotBrowserRecorder({
      scheduler,
      clock: new FakeClock().now,
    });
    recorder.attachPeerConnection(pc);
    await scheduler.fireAll(1);
    await recorder.requestMicrophone(mediaDevices);

    const serialized = JSON.stringify(recorder.drain());

    // Raw identifiers/addresses are absent...
    expect(serialized).not.toContain(label);
    expect(serialized).not.toContain(deviceId);
    expect(serialized).not.toContain("203.0.113.7");
    // ...but the opaque hash and safe telemetry survive.
    expect(serialized).toMatch(/dev_[0-9a-f]{8}/);
    expect(serialized).toContain("wifi");
  });
});
