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
 * Bounds & honesty: the snapshot/event buffers are bounded — on overflow the
 * OLDEST observation is dropped and the loss is recorded as explicit coverage,
 * never lost silently. `getStats()`/permission errors and overlapping samples
 * are likewise recorded as coverage, not swallowed.
 *
 * Clock honesty: every buffered `timestamp_ms` is a RAW reading of the injected
 * monotonic clock (not rebased to zero), tagged by a stable per-recorder
 * `clockDomain` id so the server keeps browser time in its own clock domain.
 *
 * Trace honesty: the recorder JOINS an application-supplied trace context when
 * given one and only mints its own when none is supplied — it never overwrites
 * the app's trace.
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
import {
  defaultClock,
  defaultRandomSource,
  defaultScheduler,
  defaultWallOriginMs,
} from "./env.js";
import { makeSalt, opaqueDeviceId } from "./privacy.js";
import {
  createTraceContext,
  injectTraceHeaders,
  parseTraceParent,
} from "./trace-context.js";
import type {
  AudioContextLike,
  BrowserClockDomain,
  CaptureCoverage,
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
  /**
   * Join the application's existing trace instead of minting a new one. Supply
   * either a full `TraceContext` or the raw `traceparent` header — the recorder
   * propagates it and never overwrites it. A new context is minted only when
   * neither is supplied (or the supplied `traceparent` is not spec-valid).
   */
  traceContext?: TraceContext;
  traceparent?: string;
  /** Max buffered `getStats` snapshots before the oldest is dropped (default 1024). */
  maxSnapshots?: number;
  /** Max buffered device events before the oldest is dropped (default 1024). */
  maxDeviceEvents?: number;
  /** The injected clock's reading uncertainty, in ms (default 1). */
  clockUncertaintyMs?: number;
  /**
   * Unix-epoch wall time (ms) at the injected clock's origin
   * (`performance.timeOrigin`). Carried so a declared client<->server calibration
   * has a wall timestamp to align. Pass `null` to carry only the monotonic
   * reading (no wall calibration possible). Defaults from the host performance
   * clock.
   */
  wallOriginMs?: number | null;
}

export interface AttachPeerConnectionOptions {
  /** Sampling period in ms (default 1000). Must be a positive finite number. */
  intervalMs?: number;
}

export interface ObserveMediaDevicesOptions {
  /** Optional Permissions API to read + watch the `microphone` permission. */
  permissions?: PermissionsLike;
}

const DEFAULT_SAMPLE_INTERVAL_MS = 1000;
const DEFAULT_MAX_SNAPSHOTS = 1024;
const DEFAULT_MAX_DEVICE_EVENTS = 1024;
const DEFAULT_CLOCK_UNCERTAINTY_MS = 1;

/** Mutable per-window coverage counters (reset each `drain()`). */
interface CoverageCounters {
  droppedSnapshots: number;
  droppedDeviceEvents: number;
  statsErrors: number;
  statsOverlaps: number;
  permissionErrors: number;
}

function zeroCounters(): CoverageCounters {
  return {
    droppedSnapshots: 0,
    droppedDeviceEvents: 0,
    statsErrors: 0,
    statsOverlaps: 0,
    permissionErrors: 0,
  };
}

export class EarshotBrowserRecorder {
  readonly sessionId: string;
  readonly clockDomainId: string;

  private readonly clock: Clock;
  private readonly scheduler: Scheduler;
  private readonly random: RandomSource;
  private readonly salt: string;
  private readonly trace: TraceContext;
  private readonly maxSnapshots: number;
  private readonly maxDeviceEvents: number;
  private readonly clockUncertaintyMs: number;
  private readonly wallOriginMs: number | null;

  private snapshots: WebRtcSnapshot[] = [];
  private deviceEvents: DeviceEvent[] = [];
  private counters: CoverageCounters = zeroCounters();
  private readonly teardowns: Array<() => void> = [];
  private stopped = false;

  constructor(options: BrowserRecorderOptions = {}) {
    this.clock = options.clock ?? defaultClock;
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.random = options.random ?? defaultRandomSource;
    this.salt = makeSalt(this.random);
    this.sessionId = options.sessionId ?? `sess_${makeSalt(this.random, 8)}`;
    this.clockDomainId = `clk_${makeSalt(this.random, 8)}`;
    this.trace = this.resolveTrace(options);
    this.maxSnapshots = this.positiveIntOption(
      options.maxSnapshots,
      DEFAULT_MAX_SNAPSHOTS,
    );
    this.maxDeviceEvents = this.positiveIntOption(
      options.maxDeviceEvents,
      DEFAULT_MAX_DEVICE_EVENTS,
    );
    this.clockUncertaintyMs = this.nonNegativeOption(
      options.clockUncertaintyMs,
      DEFAULT_CLOCK_UNCERTAINTY_MS,
    );
    this.wallOriginMs =
      options.wallOriginMs === undefined ? defaultWallOriginMs() : options.wallOriginMs;
  }

  // -- trace context ---------------------------------------------------------

  /** The session's W3C trace-context (stable for the recorder's lifetime). */
  traceContext(): TraceContext {
    return this.trace;
  }

  /**
   * Return headers with `traceparent` set (does not mutate the input). An
   * existing `traceparent` in the passed headers is preserved, never clobbered.
   */
  injectTraceHeaders(headers?: Record<string, string>): Record<string, string> {
    return injectTraceHeaders(this.trace, headers);
  }

  // -- WebRTC ----------------------------------------------------------------

  /**
   * Periodically sample `pc.getStats()` and buffer normalised snapshots. Each
   * sample is timestamped with the injected clock at the moment the report
   * resolves. A failed `getStats()` is recorded as coverage (never fatal, never
   * silent), and an overlapping sample — one that would start while the previous
   * `getStats()` is still in flight — is skipped and recorded, not run
   * concurrently.
   */
  attachPeerConnection(
    pc: PeerConnectionLike,
    options: AttachPeerConnectionOptions = {},
  ): void {
    const intervalMs = options.intervalMs ?? DEFAULT_SAMPLE_INTERVAL_MS;
    if (
      typeof intervalMs !== "number" ||
      !Number.isFinite(intervalMs) ||
      intervalMs <= 0
    ) {
      throw new RangeError(
        `attachPeerConnection: intervalMs must be a positive finite number (got ${String(
          intervalMs,
        )})`,
      );
    }
    if (this.stopped) return;
    let inFlight = false;
    const sample = async (): Promise<void> => {
      if (this.stopped) return;
      if (inFlight) {
        // A previous getStats() has not resolved: skip rather than overlap.
        this.counters.statsOverlaps += 1;
        return;
      }
      inFlight = true;
      try {
        const report = await pc.getStats();
        if (this.stopped) return;
        this.pushSnapshot(normalizeStatsReport(report, this.clock()));
      } catch {
        // A getStats() rejection is an explicit coverage gap, not a crash.
        this.counters.statsErrors += 1;
      } finally {
        inFlight = false;
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
    if (latency) this.pushDeviceEvent(latency);
    this.pushDeviceEvent(audioContextStateEvent(ctx.state, this.clock()));

    const onStateChange = (): void => {
      if (this.stopped) return;
      this.pushDeviceEvent(audioContextStateEvent(ctx.state, this.clock()));
    };
    ctx.addEventListener("statechange", onStateChange);
    this.teardowns.push(() => ctx.removeEventListener("statechange", onStateChange));

    const onSinkChange = (): void => {
      if (this.stopped) return;
      const sinkHash = opaqueDeviceId(sinkIdToString(ctx.sinkId), this.salt, "sink");
      this.pushDeviceEvent(sinkChangeEvent(this.clock(), sinkHash));
    };
    ctx.addEventListener("sinkchange", onSinkChange);
    this.teardowns.push(() => ctx.removeEventListener("sinkchange", onSinkChange));
  }

  // -- media devices / permissions -------------------------------------------

  /**
   * Watch `devicechange`, and (when a Permissions API is supplied) read and
   * watch the `microphone` permission. Returns once the initial permission
   * query settles, so callers/tests can await a deterministic first reading. A
   * rejected permission query is recorded as coverage, never swallowed silently.
   */
  async observeMediaDevices(
    mediaDevices: MediaDevicesLike,
    options: ObserveMediaDevicesOptions = {},
  ): Promise<void> {
    if (this.stopped) return;

    const onDeviceChange = (): void => {
      if (this.stopped) return;
      this.pushDeviceEvent(deviceChangeEvent(this.clock()));
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
      this.pushDeviceEvent(permissionEvent(status.state, this.clock()));
      const onChange = (): void => {
        if (this.stopped) return;
        this.pushDeviceEvent(permissionEvent(status.state, this.clock()));
      };
      status.addEventListener("change", onChange);
      this.teardowns.push(() => status.removeEventListener("change", onChange));
    } catch {
      // Some browsers reject `query({name:"microphone"})`; record the gap
      // explicitly rather than dropping it on the floor.
      this.counters.permissionErrors += 1;
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
      this.pushDeviceEvent(granted);
      for (const track of tracks) this.trackAudioTrack(track);
      return stream;
    } catch (error) {
      const state = classifyPermissionError(error);
      if (state) this.pushDeviceEvent(permissionEvent(state, this.clock()));
      return null;
    }
  }

  // -- app-detected signals (no W3C event exists for these) ------------------

  /** Record a configured-vs-actual sample-rate mismatch the app detected. */
  recordSampleRateMismatch(configuredHz: number, actualHz: number): void {
    if (this.stopped) return;
    this.pushDeviceEvent(sampleRateMismatchEvent(configuredHz, actualHz, this.clock()));
  }

  /** Record a render buffer under-run / glitch / dropped-frame the app detected. */
  recordRenderGlitch(kind: "underrun" | "dropped_frames" | "glitch" = "underrun"): void {
    if (this.stopped) return;
    this.pushDeviceEvent(underrunEvent(this.clock(), kind));
  }

  // -- drain / stop ----------------------------------------------------------

  /**
   * Hand the buffered payload to the caller and reset the buffers, so the next
   * POST starts clean. The session id, trace-context and clock-domain id are
   * stable across drains (so the browser timeline is continuous, not restarted),
   * while the per-window coverage counters reset each drain.
   */
  drain(): CapturePayload {
    const payload: CapturePayload = {
      sessionId: this.sessionId,
      traceContext: this.trace,
      clockDomain: this.clockDomain(),
      snapshots: this.snapshots,
      deviceEvents: this.deviceEvents,
      coverage: this.buildCoverage(),
    };
    this.snapshots = [];
    this.deviceEvents = [];
    this.counters = zeroCounters();
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

  private clockDomain(): BrowserClockDomain {
    return {
      id: this.clockDomainId,
      kind: "browser_monotonic",
      unit: "ms",
      uncertaintyMs: this.clockUncertaintyMs,
      wallOriginMs: this.wallOriginMs,
    };
  }

  private pushSnapshot(snapshot: WebRtcSnapshot): void {
    if (this.snapshots.length >= this.maxSnapshots) {
      this.snapshots.shift(); // drop the OLDEST, keep the most recent window
      this.counters.droppedSnapshots += 1;
    }
    this.snapshots.push(snapshot);
  }

  private pushDeviceEvent(event: DeviceEvent): void {
    if (this.deviceEvents.length >= this.maxDeviceEvents) {
      this.deviceEvents.shift();
      this.counters.droppedDeviceEvents += 1;
    }
    this.deviceEvents.push(event);
  }

  private buildCoverage(): CaptureCoverage[] {
    const coverage: CaptureCoverage[] = [];
    const { counters } = this;
    if (counters.droppedSnapshots > 0) {
      coverage.push({
        signal: "webrtc.snapshots",
        availability: "partial",
        reason: "buffer_overflow_oldest_dropped",
        droppedCount: counters.droppedSnapshots,
      });
    }
    if (counters.droppedDeviceEvents > 0) {
      coverage.push({
        signal: "device.events",
        availability: "partial",
        reason: "buffer_overflow_oldest_dropped",
        droppedCount: counters.droppedDeviceEvents,
      });
    }
    if (counters.statsErrors > 0) {
      coverage.push({
        signal: "webrtc.getstats",
        availability: "partial",
        reason: "getstats_failed",
        droppedCount: counters.statsErrors,
      });
    }
    if (counters.statsOverlaps > 0) {
      coverage.push({
        signal: "webrtc.getstats_overlap",
        availability: "partial",
        reason: "overlapping_sample_skipped",
        droppedCount: counters.statsOverlaps,
      });
    }
    if (counters.permissionErrors > 0) {
      coverage.push({
        signal: "device.permission_query",
        availability: "not_observed",
        reason: "permission_query_failed",
        droppedCount: counters.permissionErrors,
      });
    }
    return coverage;
  }

  private resolveTrace(options: BrowserRecorderOptions): TraceContext {
    if (options.traceContext) return options.traceContext;
    const joined = parseTraceParent(options.traceparent);
    if (joined) return joined;
    return createTraceContext(this.random);
  }

  private positiveIntOption(value: number | undefined, fallback: number): number {
    if (value === undefined) return fallback;
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
      throw new RangeError(
        `buffer bound must be a positive finite number (got ${String(value)})`,
      );
    }
    return Math.floor(value);
  }

  private nonNegativeOption(value: number | undefined, fallback: number): number {
    if (value === undefined) return fallback;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      throw new RangeError(
        `clockUncertaintyMs must be a finite non-negative number (got ${String(value)})`,
      );
    }
    return value;
  }

  private trackAudioTrack(track: MediaTrackLike): void {
    const deviceHash = opaqueDeviceId(track.getSettings?.().deviceId, this.salt);
    const onEnded = (): void => {
      if (this.stopped) return;
      this.pushDeviceEvent(deviceChangeEvent(this.clock(), deviceHash));
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
