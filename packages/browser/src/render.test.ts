/**
 * Capture-to-render instrumentation: only what the platform actually exposes.
 *
 * Every signal asserted here has a real W3C source — `AudioContext`'s
 * `getOutputTimestamp()`/`currentTime`/`sampleRate`, `MediaTrackSettings`, and
 * the `media-playout` (`RTCAudioPlayoutStats`) and jitter-buffer/processing
 * members of `getStats()`. The counterpart assertions matter just as much: where
 * a browser does not expose a signal, the recorder must emit an explicit
 * coverage note rather than a plausible-looking number.
 */

import { describe, expect, it } from "vitest";

import { EarshotBrowserRecorder } from "./recorder.js";
import type { CaptureCoverage, DeviceEvent } from "./types.js";
import {
  FakeAudioContext,
  FakeClock,
  FakeMediaDevices,
  FakeMediaStream,
  FakeMediaTrack,
  FakePeerConnection,
  FakeScheduler,
  makeStatsReport,
} from "./testing/fakes.js";

function newRecorder(scheduler: FakeScheduler): EarshotBrowserRecorder {
  return new EarshotBrowserRecorder({ scheduler, clock: new FakeClock(1000, 10).now });
}

function coverageFor(
  notes: CaptureCoverage[],
  signal: string,
): CaptureCoverage | undefined {
  return notes.find((note) => note.signal === signal);
}

function latencyEvents(events: DeviceEvent[]): DeviceEvent[] {
  return events.filter((event) => event.type === "latency");
}

describe("render queue depth (AudioContext.getOutputTimestamp)", () => {
  it("records currentTime - contextTime as the queued render seconds", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    const ctx = new FakeAudioContext({
      baseLatency: 0.005,
      outputLatency: 0.02,
      currentTime: 12.5,
      contextTime: 12.44,
    });

    recorder.attachAudioContext(ctx);
    ctx.setRenderPosition(13.5, 13.3);
    await scheduler.fireAll(1);

    const events = latencyEvents(recorder.drain().deviceEvents);
    expect(events).toHaveLength(2);
    // Attach reading: base + output + the queue depth at that moment.
    expect(events[0]!.base_latency_s).toBe(0.005);
    expect(events[0]!.render_queue_s).toBeCloseTo(0.06, 10);
    // Periodic reading: the depth moved, and it is re-read, not re-used.
    expect(events[1]!.render_queue_s).toBeCloseTo(0.2, 10);
    expect(events[1]!.output_latency_s).toBe(0.02);
    expect(events[1]!.base_latency_s).toBeUndefined(); // constant; not re-asserted
  });

  it("says the signal is unavailable rather than reporting a zero queue", () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    // `contextTime: null` models a browser with no getOutputTimestamp at all.
    const ctx = new FakeAudioContext({ baseLatency: 0.005, contextTime: null });

    recorder.attachAudioContext(ctx);
    const payload = recorder.drain();

    expect(latencyEvents(payload.deviceEvents)[0]!.render_queue_s).toBeUndefined();
    expect(coverageFor(payload.coverage, "audio.render_timing")).toEqual({
      signal: "audio.render_timing",
      availability: "not_observed",
      reason: "getoutputtimestamp_unavailable",
    });
    // No point scheduling a sampler for a method that does not exist.
    expect(scheduler.activeCount).toBe(0);
  });

  it("reports an unpopulated playout position as a partial gap", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    // The method exists but has no position yet (no audio has flowed).
    const ctx = new FakeAudioContext({ currentTime: 0, contextTime: undefined });

    recorder.attachAudioContext(ctx);
    await scheduler.fireAll(2);
    const payload = recorder.drain();

    expect(latencyEvents(payload.deviceEvents)).toHaveLength(0);
    expect(coverageFor(payload.coverage, "audio.render_timing")).toEqual({
      signal: "audio.render_timing",
      availability: "partial",
      reason: "output_timestamp_unpopulated",
      droppedCount: 2,
    });
  });

  it("never reports a negative queue depth", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    // A playout position ahead of the graph clock is not a negative queue; it is
    // a reading we cannot interpret.
    const ctx = new FakeAudioContext({ currentTime: 1, contextTime: 2 });
    recorder.attachAudioContext(ctx);
    await scheduler.fireAll(1);
    const payload = recorder.drain();
    expect(latencyEvents(payload.deviceEvents)).toHaveLength(0);
    expect(coverageFor(payload.coverage, "audio.render_timing")?.availability).toBe(
      "partial",
    );
  });

  it("rejects a nonsensical render-timing interval", () => {
    const recorder = newRecorder(new FakeScheduler());
    for (const renderTimingIntervalMs of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() =>
        recorder.attachAudioContext(new FakeAudioContext({}), { renderTimingIntervalMs }),
      ).toThrow(RangeError);
    }
  });
});

describe("sample-rate mismatch (AudioContext.sampleRate vs MediaTrackSettings)", () => {
  it("detects a capture rate the graph is not running at", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    recorder.attachAudioContext(new FakeAudioContext({ sampleRate: 48000 }));
    const devices = new FakeMediaDevices({
      stream: new FakeMediaStream([
        new FakeMediaTrack({ deviceId: "mic-1", sampleRate: 44100 }),
      ]),
    });

    await recorder.requestMicrophone(devices);

    const mismatch = recorder
      .drain()
      .deviceEvents.find((event) => event.type === "sample_rate_mismatch");
    expect(mismatch).toMatchObject({ configured_hz: 48000, actual_hz: 44100 });
  });

  it("claims nothing when the two rates agree or either is unknown", async () => {
    const scheduler = new FakeScheduler();
    const matching = newRecorder(scheduler);
    matching.attachAudioContext(new FakeAudioContext({ sampleRate: 48000 }));
    await matching.requestMicrophone(
      new FakeMediaDevices({
        stream: new FakeMediaStream([new FakeMediaTrack({ sampleRate: 48000 })]),
      }),
    );
    expect(
      matching
        .drain()
        .deviceEvents.some((event) => event.type === "sample_rate_mismatch"),
    ).toBe(false);

    const unknown = newRecorder(new FakeScheduler());
    unknown.attachAudioContext(new FakeAudioContext({ sampleRate: 48000 }));
    await unknown.requestMicrophone(
      new FakeMediaDevices({ stream: new FakeMediaStream([new FakeMediaTrack({})]) }),
    );
    expect(
      unknown.drain().deviceEvents.some((event) => event.type === "sample_rate_mismatch"),
    ).toBe(false);
  });
});

describe("WebRTC render-path stats", () => {
  const playoutReport = () =>
    makeStatsReport({
      playout: {
        id: "playout",
        type: "media-playout",
        kind: "audio",
        totalPlayoutDelay: 3.2,
        totalSamplesCount: 96000,
        synthesizedSamplesDuration: 0.02,
        synthesizedSamplesEvents: 1,
        totalSamplesDuration: 2,
      },
      inbound: {
        id: "inbound",
        type: "inbound-rtp",
        kind: "audio",
        jitterBufferDelay: 4.2,
        jitterBufferEmittedCount: 40000,
        jitterBufferTargetDelay: 5,
        jitterBufferMinimumDelay: 1,
        jitterBufferFlushes: 2,
        totalProcessingDelay: 1.5,
        concealmentEvents: 3,
        silentConcealedSamples: 10,
        insertedSamplesForDeceleration: 4,
        removedSamplesForAcceleration: 5,
        packetsDiscarded: 1,
      },
    });

  it("carries the playout and jitter-buffer members the server engine reads", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    recorder.attachPeerConnection(new FakePeerConnection([playoutReport()]));
    await scheduler.fireAll(1);

    const stats = recorder.drain().snapshots[0]!.stats;
    expect(stats.playout).toEqual({
      id: "playout",
      type: "media-playout",
      kind: "audio",
      totalPlayoutDelay: 3.2,
      totalSamplesCount: 96000,
      synthesizedSamplesDuration: 0.02,
      synthesizedSamplesEvents: 1,
      totalSamplesDuration: 2,
    });
    expect(stats.inbound!.totalProcessingDelay).toBe(1.5);
    expect(stats.inbound!.jitterBufferTargetDelay).toBe(5);
    expect(stats.inbound!.jitterBufferFlushes).toBe(2);
  });

  it("declares audio decode time unobservable instead of implying it", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    recorder.attachPeerConnection(new FakePeerConnection([playoutReport()]));
    await scheduler.fireAll(1);

    // W3C webrtc-stats defines totalDecodeTime/framesDecoded for video only.
    expect(coverageFor(recorder.drain().coverage, "webrtc.audio_decode_time")).toEqual({
      signal: "webrtc.audio_decode_time",
      availability: "not_observed",
      reason: "decode_time_is_video_only_in_w3c_stats",
    });
  });

  it("declares a missing media-playout stat rather than assuming a clean render", async () => {
    const scheduler = new FakeScheduler();
    const recorder = newRecorder(scheduler);
    // A browser that reports inbound-rtp but not the playout stats at all.
    recorder.attachPeerConnection(
      new FakePeerConnection([
        makeStatsReport({
          inbound: {
            id: "inbound",
            type: "inbound-rtp",
            kind: "audio",
            packetsReceived: 10,
          },
        }),
      ]),
    );
    await scheduler.fireAll(1);
    const coverage = recorder.drain().coverage;

    expect(coverageFor(coverage, "webrtc.playout")).toEqual({
      signal: "webrtc.playout",
      availability: "not_observed",
      reason: "media_playout_stat_not_exposed",
    });
    expect(coverageFor(coverage, "webrtc.processing_delay")).toEqual({
      signal: "webrtc.processing_delay",
      availability: "not_observed",
      reason: "member_not_exposed",
    });
  });

  it("does not claim a platform gap for a session that never sampled stats", () => {
    const coverage = newRecorder(new FakeScheduler()).drain().coverage;
    expect(coverageFor(coverage, "webrtc.playout")).toBeUndefined();
    expect(coverageFor(coverage, "webrtc.audio_decode_time")).toBeUndefined();
  });
});

describe("caller-supplied coverage is bounded and merged", () => {
  it("merges repeats and records overflow instead of growing without limit", () => {
    const recorder = newRecorder(new FakeScheduler());
    recorder.recordCoverage({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: 2,
    });
    recorder.recordCoverage({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: 3,
    });
    for (let i = 0; i < 64; i += 1) {
      recorder.recordCoverage({
        signal: `capture.other_${i}`,
        availability: "partial",
        reason: "upload_failed_payload_dropped",
      });
    }
    const coverage = recorder.drain().coverage;

    expect(coverageFor(coverage, "capture.upload")?.droppedCount).toBe(5);
    expect(coverageFor(coverage, "capture.coverage")).toMatchObject({
      availability: "partial",
      reason: "coverage_buffer_overflow",
    });
    // The buffer itself never exceeded its bound.
    expect(
      coverage.filter((note) => note.signal.startsWith("capture.other_")).length,
    ).toBe(31);
  });

  it("refuses a coverage note with no signal", () => {
    const recorder = newRecorder(new FakeScheduler());
    expect(() =>
      recorder.recordCoverage({ signal: "", availability: "partial", reason: "x" }),
    ).toThrow(TypeError);
  });

  it("clears pending coverage once drained", () => {
    const recorder = newRecorder(new FakeScheduler());
    recorder.recordCoverage({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
    });
    expect(recorder.drain().coverage).toHaveLength(1);
    expect(recorder.drain().coverage).toHaveLength(0);
  });
});
