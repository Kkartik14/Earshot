import { describe, expect, it } from "vitest";

import { createBrowserRecorder, EarshotBrowserRecorder } from "./recorder.js";
import {
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
