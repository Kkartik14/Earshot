import { describe, expect, it } from "vitest";

import { classifyPermissionError, latencyEvent, sinkIdToString } from "./device.js";
import { createBrowserRecorder } from "./recorder.js";
import type { DeviceEvent } from "./types.js";
import {
  FakeAudioContext,
  FakeClock,
  FakeMediaDevices,
  FakeMediaStream,
  FakeMediaTrack,
  FakePermissions,
  FakePermissionStatus,
  sequentialRandom,
} from "./testing/fakes.js";

function eventsOfType(events: DeviceEvent[], type: string): DeviceEvent[] {
  return events.filter((event) => event.type === type);
}

describe("attachAudioContext", () => {
  it("records base+output latency (seconds) and the initial state", () => {
    const ctx = new FakeAudioContext({
      state: "running",
      baseLatency: 0.005,
      outputLatency: 0.02,
    });
    const recorder = createBrowserRecorder({ clock: new FakeClock().now });

    recorder.attachAudioContext(ctx);
    const { deviceEvents } = recorder.drain();

    const latency = eventsOfType(deviceEvents, "latency")[0];
    expect(latency).toMatchObject({
      type: "latency",
      base_latency_s: 0.005,
      output_latency_s: 0.02,
    });
    const state = eventsOfType(deviceEvents, "audiocontext_state")[0];
    expect(state).toMatchObject({ type: "audiocontext_state", state: "running" });
  });

  it("emits an audiocontext_state event on a suspend transition", () => {
    const ctx = new FakeAudioContext({ state: "running" });
    const recorder = createBrowserRecorder();
    recorder.attachAudioContext(ctx);

    ctx.setState("suspended");
    const suspended = eventsOfType(recorder.drain().deviceEvents, "audiocontext_state");

    expect(suspended.some((event) => event.state === "suspended")).toBe(true);
  });

  it("emits a sink_change with an opaque hash (never the raw sink id)", () => {
    const ctx = new FakeAudioContext({ state: "running", sinkId: "" });
    const recorder = createBrowserRecorder();
    recorder.attachAudioContext(ctx);

    ctx.setSink("hw:CARD=Headset,DEV=0");
    const sink = eventsOfType(recorder.drain().deviceEvents, "sink_change")[0];

    expect(sink).toBeDefined();
    expect(sink!.sinkHash).toMatch(/^sink_[0-9a-f]{8}$/);
    expect(JSON.stringify(sink)).not.toContain("Headset");
  });
});

describe("media devices + permissions", () => {
  it("records a denied permission from a getUserMedia NotAllowedError", async () => {
    const mediaDevices = new FakeMediaDevices({ error: { name: "NotAllowedError" } });
    const recorder = createBrowserRecorder();

    const stream = await recorder.requestMicrophone(mediaDevices);
    const { deviceEvents } = recorder.drain();

    expect(stream).toBeNull();
    expect(eventsOfType(deviceEvents, "permission")).toEqual([
      expect.objectContaining({ type: "permission", state: "denied" }),
    ]);
  });

  it("records a granted permission (opaque device hash) and track lifecycle", async () => {
    const track = new FakeMediaTrack({ deviceId: "device-abc", label: "Studio Mic" });
    const stream = new FakeMediaStream([track]);
    const mediaDevices = new FakeMediaDevices({ stream });
    const recorder = createBrowserRecorder({ random: sequentialRandom(7) });

    await recorder.requestMicrophone(mediaDevices);
    track.end(); // the underlying device goes away
    const { deviceEvents } = recorder.drain();

    const granted = eventsOfType(deviceEvents, "permission")[0];
    expect(granted).toMatchObject({ type: "permission", state: "granted" });
    expect(granted!.deviceHash).toMatch(/^dev_[0-9a-f]{8}$/);
    expect(eventsOfType(deviceEvents, "devicechange")).toHaveLength(1);
  });

  it("observes devicechange events", async () => {
    const mediaDevices = new FakeMediaDevices();
    const recorder = createBrowserRecorder();
    await recorder.observeMediaDevices(mediaDevices);

    mediaDevices.triggerDeviceChange();

    expect(eventsOfType(recorder.drain().deviceEvents, "devicechange")).toHaveLength(1);
  });

  it("reads and watches the microphone permission via the Permissions API", async () => {
    const status = new FakePermissionStatus("granted");
    const permissions = new FakePermissions(status);
    const mediaDevices = new FakeMediaDevices();
    const recorder = createBrowserRecorder();

    await recorder.observeMediaDevices(mediaDevices, { permissions });
    status.setState("denied"); // user revokes mid-call
    const perms = eventsOfType(recorder.drain().deviceEvents, "permission");

    expect(perms.map((event) => event.state)).toEqual(["granted", "denied"]);
  });
});

describe("app-detected signals", () => {
  it("records a sample-rate mismatch in the server shape", () => {
    const recorder = createBrowserRecorder();
    recorder.recordSampleRateMismatch(48000, 44100);

    const event = recorder.drain().deviceEvents[0];
    expect(event).toMatchObject({
      type: "sample_rate_mismatch",
      configured_hz: 48000,
      actual_hz: 44100,
    });
  });

  it("records a render under-run", () => {
    const recorder = createBrowserRecorder();
    recorder.recordRenderGlitch("dropped_frames");

    expect(recorder.drain().deviceEvents[0]).toMatchObject({ type: "dropped_frames" });
  });
});

describe("pure builders", () => {
  it("classifies only permission errors as denied", () => {
    expect(classifyPermissionError({ name: "NotAllowedError" })).toBe("denied");
    expect(classifyPermissionError({ name: "SecurityError" })).toBe("denied");
    expect(classifyPermissionError({ name: "NotFoundError" })).toBeNull();
    expect(classifyPermissionError(new Error("boom"))).toBeNull();
  });

  it("omits an absent latency rather than reporting zero", () => {
    expect(latencyEvent(undefined, undefined, 1)).toBeNull();
    expect(latencyEvent(0.004, undefined, 1)).toMatchObject({ base_latency_s: 0.004 });
    expect("output_latency_s" in latencyEvent(0.004, undefined, 1)!).toBe(false);
  });

  it("normalises the default-sink object form", () => {
    expect(sinkIdToString({ type: "none" })).toBe("type:none");
    expect(sinkIdToString("")).toBeUndefined();
    expect(sinkIdToString("device-1")).toBe("device-1");
  });
});
