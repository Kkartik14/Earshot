/**
 * `EarshotBrowserRecorder` — the client-side capture kernel.
 *
 * It observes the live W3C APIs (`RTCPeerConnection.getStats()`, `AudioContext`
 * state/latency/sink, `navigator.mediaDevices` + Permissions) and buffers the
 * results in the EXACT shapes the server engines consume, then `drain()`s a
 * `CapturePayload` the client POSTs. The server feeds `snapshots` to
 * `analyze_webrtc_stats` and `deviceEvents` to `analyze_audio_graph`.
 *
 * Design seams (all injectable, all defaulted): a monotonic `clock`, an interval
 * `scheduler`, and a `random` source. Injecting them is what lets the tests run
 * the whole kernel deterministically against mocked browser APIs.
 *
 * Privacy posture: metadata only. No audio samples are ever read or retained
 * (the observed APIs do not even expose them). Device labels/ids and ICE
 * candidate addresses never leave the client — device ids become opaque
 * per-session salted hashes and candidate addresses are scrubbed.
 */

import {
  audioContextStateEvent,
  classifyPermissionError,
  deviceChangeEvent,
  latencyEvent,
  permissionEvent,
  sampleRateMismatchEvent,
  sinkChangeEvent,
  sinkIdToString,
  underrunEvent,
} from "./device.js";
import { defaultClock, defaultRandomSource, defaultScheduler } from "./env.js";
import { makeSalt, opaqueDeviceId } from "./privacy.js";
import { createTraceContext, injectTraceHeaders } from "./trace-context.js";
import type {
  AudioContextLike,
  CapturePayload,
  Clock,
  DeviceEvent,
  MediaDevicesLike,
  MediaStreamLike,
  MediaTrackLike,
  PeerConnectionLike,
  PermissionsLike,
  RandomSource,
  Scheduler,
  TraceContext,
  WebRtcSnapshot,
} from "./types.js";
import { normalizeStatsReport } from "./webrtc.js";

export interface BrowserRecorderOptions {
  /** Session correlation id; a random `sess_<hex>` is minted when omitted. */
  sessionId?: string;
  /** Monotonic ms clock (default `performance.now()` / `Date.now()`). */
  clock?: Clock;
  /** Interval scheduler (default host `setInterval`/`clearInterval`). */
  scheduler?: Scheduler;
  /** Randomness for trace ids + privacy salt (default Web Crypto). */
  random?: RandomSource;
}

export interface AttachPeerConnectionOptions {
  /** Sampling period in ms (default 1000). */
  intervalMs?: number;
}

export interface ObserveMediaDevicesOptions {
  /** Optional Permissions API to read + watch the `microphone` permission. */
  permissions?: PermissionsLike;
}

const DEFAULT_SAMPLE_INTERVAL_MS = 1000;

export class EarshotBrowserRecorder {
  readonly sessionId: string;

  private readonly clock: Clock;
  private readonly scheduler: Scheduler;
  private readonly random: RandomSource;
  private readonly salt: string;
  private readonly trace: TraceContext;

  private snapshots: WebRtcSnapshot[] = [];
  private deviceEvents: DeviceEvent[] = [];
  private readonly teardowns: Array<() => void> = [];
  private stopped = false;

  constructor(options: BrowserRecorderOptions = {}) {
    this.clock = options.clock ?? defaultClock;
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.random = options.random ?? defaultRandomSource;
    this.salt = makeSalt(this.random);
    this.sessionId = options.sessionId ?? `sess_${makeSalt(this.random, 8)}`;
    this.trace = createTraceContext(this.random);
  }

  // -- trace context ---------------------------------------------------------

  /** The session's W3C trace-context (stable for the recorder's lifetime). */
  traceContext(): TraceContext {
    return this.trace;
  }

  /** Return headers with `traceparent` set (does not mutate the input). */
  injectTraceHeaders(headers?: Record<string, string>): Record<string, string> {
    return injectTraceHeaders(this.trace, headers);
  }

  // -- WebRTC ----------------------------------------------------------------

  /**
   * Periodically sample `pc.getStats()` and buffer normalised snapshots. Each
   * sample is timestamped with the injected clock at the moment the report
   * resolves. A failed `getStats()` is treated as unknown (skipped), never fatal.
   */
  attachPeerConnection(
    pc: PeerConnectionLike,
    options: AttachPeerConnectionOptions = {},
  ): void {
    if (this.stopped) return;
    const intervalMs = options.intervalMs ?? DEFAULT_SAMPLE_INTERVAL_MS;
    const sample = async (): Promise<void> => {
      try {
        const report = await pc.getStats();
        if (this.stopped) return;
        this.snapshots.push(normalizeStatsReport(report, this.clock()));
      } catch {
        // fail-open: a getStats() rejection is a coverage gap, not a crash.
      }
    };
    const handle = this.scheduler.setInterval(sample, intervalMs);
    this.teardowns.push(() => this.scheduler.clearInterval(handle));
  }

  // -- Web Audio -------------------------------------------------------------

  /**
   * Observe an `AudioContext`: record its current latency + state, then every
   * `statechange` and `sinkchange`. `baseLatency` is deterministic (measured);
   * `outputLatency` is a W3C estimate — both are surfaced and the server keeps
   * the distinction.
   */
  attachAudioContext(ctx: AudioContextLike): void {
    if (this.stopped) return;

    const latency = latencyEvent(ctx.baseLatency, ctx.outputLatency, this.clock());
    if (latency) this.deviceEvents.push(latency);
    this.deviceEvents.push(audioContextStateEvent(ctx.state, this.clock()));

    const onStateChange = (): void => {
      if (this.stopped) return;
      this.deviceEvents.push(audioContextStateEvent(ctx.state, this.clock()));
    };
    ctx.addEventListener("statechange", onStateChange);
    this.teardowns.push(() => ctx.removeEventListener("statechange", onStateChange));

    const onSinkChange = (): void => {
      if (this.stopped) return;
      const sinkHash = opaqueDeviceId(sinkIdToString(ctx.sinkId), this.salt, "sink");
      this.deviceEvents.push(sinkChangeEvent(this.clock(), sinkHash));
    };
    ctx.addEventListener("sinkchange", onSinkChange);
    this.teardowns.push(() => ctx.removeEventListener("sinkchange", onSinkChange));
  }

  // -- media devices / permissions -------------------------------------------

  /**
   * Watch `devicechange`, and (when a Permissions API is supplied) read and
   * watch the `microphone` permission. Returns once the initial permission
   * query settles, so callers/tests can await a deterministic first reading.
   */
  async observeMediaDevices(
    mediaDevices: MediaDevicesLike,
    options: ObserveMediaDevicesOptions = {},
  ): Promise<void> {
    if (this.stopped) return;

    const onDeviceChange = (): void => {
      if (this.stopped) return;
      this.deviceEvents.push(deviceChangeEvent(this.clock()));
    };
    mediaDevices.addEventListener("devicechange", onDeviceChange);
    this.teardowns.push(() =>
      mediaDevices.removeEventListener("devicechange", onDeviceChange),
    );

    const permissions = options.permissions;
    if (!permissions) return;
    try {
      const status = await permissions.query({ name: "microphone" });
      if (this.stopped) return;
      this.deviceEvents.push(permissionEvent(status.state, this.clock()));
      const onChange = (): void => {
        if (this.stopped) return;
        this.deviceEvents.push(permissionEvent(status.state, this.clock()));
      };
      status.addEventListener("change", onChange);
      this.teardowns.push(() => status.removeEventListener("change", onChange));
    } catch {
      // Some browsers reject `query({name:"microphone"})`; fail-open (no fact).
    }
  }

  /**
   * Call `getUserMedia` and record the outcome: a `granted` permission event
   * (carrying an opaque device hash) plus per-track lifecycle on success, or a
   * `denied` permission event on a NotAllowed/Security rejection. Returns the
   * stream on success, else `null` — never throws (fail-open).
   */
  async requestMicrophone(
    mediaDevices: MediaDevicesLike,
    constraints?: unknown,
  ): Promise<MediaStreamLike | null> {
    if (this.stopped) return null;
    try {
      const stream = await mediaDevices.getUserMedia(constraints);
      if (this.stopped) return stream;
      const tracks = stream.getAudioTracks();
      const primary = tracks[0];
      const deviceHash = primary
        ? opaqueDeviceId(primary.getSettings?.().deviceId, this.salt)
        : undefined;
      const granted = permissionEvent("granted", this.clock());
      if (deviceHash) granted.deviceHash = deviceHash;
      this.deviceEvents.push(granted);
      for (const track of tracks) this.trackAudioTrack(track);
      return stream;
    } catch (error) {
      const state = classifyPermissionError(error);
      if (state) this.deviceEvents.push(permissionEvent(state, this.clock()));
      return null;
    }
  }

  // -- app-detected signals (no W3C event exists for these) ------------------

  /** Record a configured-vs-actual sample-rate mismatch the app detected. */
  recordSampleRateMismatch(configuredHz: number, actualHz: number): void {
    if (this.stopped) return;
    this.deviceEvents.push(sampleRateMismatchEvent(configuredHz, actualHz, this.clock()));
  }

  /** Record a render buffer under-run / glitch / dropped-frame the app detected. */
  recordRenderGlitch(kind: "underrun" | "dropped_frames" | "glitch" = "underrun"): void {
    if (this.stopped) return;
    this.deviceEvents.push(underrunEvent(this.clock(), kind));
  }

  // -- drain / stop ----------------------------------------------------------

  /**
   * Hand the buffered payload to the caller and reset the buffers, so the next
   * POST starts clean. The session id and trace-context are stable across drains.
   */
  drain(): CapturePayload {
    const payload: CapturePayload = {
      sessionId: this.sessionId,
      traceContext: this.trace,
      snapshots: this.snapshots,
      deviceEvents: this.deviceEvents,
    };
    this.snapshots = [];
    this.deviceEvents = [];
    return payload;
  }

  /** Stop all sampling and remove every listener. Idempotent; safe to re-call. */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    for (const teardown of this.teardowns.splice(0)) {
      try {
        teardown();
      } catch {
        // A listener/interval that is already gone is not an error here.
      }
    }
  }

  // -- internals -------------------------------------------------------------

  private trackAudioTrack(track: MediaTrackLike): void {
    const deviceHash = opaqueDeviceId(track.getSettings?.().deviceId, this.salt);
    const onEnded = (): void => {
      if (this.stopped) return;
      this.deviceEvents.push(deviceChangeEvent(this.clock(), deviceHash));
    };
    track.addEventListener("ended", onEnded);
    this.teardowns.push(() => track.removeEventListener("ended", onEnded));
  }
}

/** Functional constructor mirroring the class (parity with the SDK style). */
export function createBrowserRecorder(
  options?: BrowserRecorderOptions,
): EarshotBrowserRecorder {
  return new EarshotBrowserRecorder(options);
}
