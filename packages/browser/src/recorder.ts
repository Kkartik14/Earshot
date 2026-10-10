/** Browser capture adapter for WebRTC stats, audio contexts, and media devices.
 * `drain()` emits the payload consumed by the server's WebRTC and device engines.
 * Unsupported signals, sampling failures, and dropped observations become
 * coverage. Timestamps stay in one raw browser clock domain; device ids are
 * session-salted, and audio samples are never read. Clock, scheduler, and random
 * sources are injectable for deterministic tests.
 */

import {
  audioContextStateEvent,
  classifyPermissionError,
  deviceChangeEvent,
  latencyEvent,
  permissionEvent,
  renderQueueSeconds,
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
  CAPTURE_PROTOCOL_VERSION,
  CONTINUOUS_CAPTURE_VERSION,
  type CaptureVersion,
} from "./protocol.js";
import {
  createTraceContext,
  injectTraceHeaders,
  parseTraceParent,
} from "./trace-context.js";
import type {
  AudioContextLike,
  BrowserClockDomain,
  CaptureCoverage,
  CaptureEndReason,
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
  /**
   * The capture wire version to emit (default `1`). Opt into `2` for continuous
   * capture: every `drain()` then carries a monotonic `drainSequence` under the
   * same stable `sessionId` and `clockDomain.id`, and the server accumulates the
   * whole call into one journal-backed provisional artifact instead of a
   * per-drain incident. It stays opt-in so a client can still target a server
   * that only governs version 1.
   */
  captureVersion?: CaptureVersion;
}

export interface AttachPeerConnectionOptions {
  /** Sampling period in ms (default 1000). Must be a positive finite number. */
  intervalMs?: number;
}

export interface AttachAudioContextOptions {
  /**
   * Period in ms for sampling the render position via `getOutputTimestamp()`
   * (default 1000). Must be a positive finite number. No interval is scheduled
   * at all on a context that does not implement `getOutputTimestamp` — the
   * absence is recorded once as coverage instead.
   */
  renderTimingIntervalMs?: number;
}

export interface ObserveMediaDevicesOptions {
  /** Optional Permissions API to read + watch the `microphone` permission. */
  permissions?: PermissionsLike;
}

export interface DrainOptions {
  /**
   * Declare, on this drain, that the observer stopped — and why. `captureVersion:
   * 2` only. `call_ended` ENDS the call (the server finalizes it into a `final`
   * artifact); an abandon reason (`capture_stopped` / `page_hidden` /
   * `page_unloaded`) leaves the call provisional. Omit it on an ordinary drain.
   * Prefer `endCall()` over passing `call_ended` here.
   */
  end?: CaptureEndReason;
}

const DEFAULT_SAMPLE_INTERVAL_MS = 1000;
const DEFAULT_RENDER_TIMING_INTERVAL_MS = 1000;
const DEFAULT_MAX_SNAPSHOTS = 1024;
const DEFAULT_MAX_DEVICE_EVENTS = 1024;
const DEFAULT_CLOCK_UNCERTAINTY_MS = 1;
/** Upper bound on caller-supplied coverage notes buffered between drains. */
const MAX_PENDING_COVERAGE = 32;

/** Mutable per-window coverage counters (reset each `drain()`). */
interface CoverageCounters {
  droppedSnapshots: number;
  droppedDeviceEvents: number;
  statsErrors: number;
  statsOverlaps: number;
  permissionErrors: number;
  renderTimingUnpopulated: number;
  droppedCoverageNotes: number;
}

function zeroCounters(): CoverageCounters {
  return {
    droppedSnapshots: 0,
    droppedDeviceEvents: 0,
    statsErrors: 0,
    statsOverlaps: 0,
    permissionErrors: 0,
    renderTimingUnpopulated: 0,
    droppedCoverageNotes: 0,
  };
}

/**
 * What the platform actually exposed in this window.
 *
 * These are not measurements — they are the record of which render-path signals
 * this browser offered, so a signal the platform does not implement becomes an
 * explicit coverage note rather than a silent absence the server would read as
 * "nothing happened".
 */
interface SignalAvailability {
  sampledStats: boolean;
  audioInbound: boolean;
  processingDelay: boolean;
  playout: boolean;
  renderTimingMissing: boolean;
}

function zeroAvailability(): SignalAvailability {
  return {
    sampledStats: false,
    audioInbound: false,
    processingDelay: false,
    playout: false,
    renderTimingMissing: false,
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
  private readonly captureVersion: CaptureVersion;
  private readonly capturerStartedAtMs: number;

  private snapshots: WebRtcSnapshot[] = [];
  private deviceEvents: DeviceEvent[] = [];
  private counters: CoverageCounters = zeroCounters();
  private availability: SignalAvailability = zeroAvailability();
  private pendingCoverage: CaptureCoverage[] = [];
  private audioContext: AudioContextLike | null = null;
  private readonly teardowns: Array<() => void> = [];
  private stopped = false;
  /** 1-based, monotonic per recorder; assigned inside `drain()` under version 2. */
  private drainSequence = 0;

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
    this.captureVersion = options.captureVersion ?? CAPTURE_PROTOCOL_VERSION;
    // The recorder's own first clock reading (its construction), not the call
    // start. Carried on every v2 drain so a later phase can bound the capture.
    this.capturerStartedAtMs = this.clock();
  }

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
        const snapshot = normalizeStatsReport(report, this.clock());
        this.noteStatsAvailability(snapshot);
        this.pushSnapshot(snapshot);
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

  /** Observe latency, render queue, state, and sink changes. Missing or
   * unpopulated `getOutputTimestamp()` values are recorded as coverage.
   */
  attachAudioContext(
    ctx: AudioContextLike,
    options: AttachAudioContextOptions = {},
  ): void {
    const renderTimingIntervalMs =
      options.renderTimingIntervalMs ?? DEFAULT_RENDER_TIMING_INTERVAL_MS;
    if (
      typeof renderTimingIntervalMs !== "number" ||
      !Number.isFinite(renderTimingIntervalMs) ||
      renderTimingIntervalMs <= 0
    ) {
      throw new RangeError(
        `attachAudioContext: renderTimingIntervalMs must be a positive finite number (got ${String(
          renderTimingIntervalMs,
        )})`,
      );
    }
    if (this.stopped) return;
    // Remembered so a capture track's settled sample rate can be compared with
    // the graph's — the one sample-rate mismatch the platform lets us observe.
    this.audioContext = ctx;

    const latency = latencyEvent(
      ctx.baseLatency,
      ctx.outputLatency,
      this.clock(),
      this.readRenderQueue(ctx),
    );
    if (latency) this.pushDeviceEvent(latency);
    this.pushDeviceEvent(audioContextStateEvent(ctx.state, this.clock()));
    this.scheduleRenderTiming(ctx, renderTimingIntervalMs);

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

  /** Watch `devicechange` and optional microphone permission changes. Resolves
   * after the initial permission query; failures are recorded as coverage.
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

  /** Request audio and record permission plus hashed device metadata. Returns
   * the stream or `null` on denial; permission errors do not propagate.
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
      if (primary) this.checkSampleRate(primary);
      for (const track of tracks) this.trackAudioTrack(track);
      return stream;
    } catch (error) {
      const state = classifyPermissionError(error);
      if (state) this.pushDeviceEvent(permissionEvent(state, this.clock()));
      return null;
    }
  }

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

  /** Queue an externally observed gap, usually a failed delivery, for the next
   * drain. Matching notes merge counts; the bounded buffer records overflow.
   */
  recordCoverage(note: CaptureCoverage): void {
    if (this.stopped) return;
    if (typeof note?.signal !== "string" || note.signal.length === 0) {
      throw new TypeError("recordCoverage: a coverage note needs a non-empty signal");
    }
    const existing = this.pendingCoverage.find(
      (item) =>
        item.signal === note.signal &&
        item.availability === note.availability &&
        item.reason === note.reason,
    );
    if (existing) {
      if (typeof note.droppedCount === "number" && Number.isFinite(note.droppedCount)) {
        existing.droppedCount = (existing.droppedCount ?? 0) + note.droppedCount;
      }
      return;
    }
    if (this.pendingCoverage.length >= MAX_PENDING_COVERAGE) {
      this.counters.droppedCoverageNotes += 1;
      return;
    }
    this.pendingCoverage.push({ ...note });
  }

  /** Return buffered observations and reset per-drain state. Session, trace, and
   * clock-domain ids persist. Version 2 accepts `{ end }` to declare why capture
   * stopped, stamped in the same raw clock domain; ordinary drains do not close it.
   */
  drain(options: DrainOptions = {}): CapturePayload {
    const payload: CapturePayload = {
      captureVersion: this.captureVersion,
      sessionId: this.sessionId,
      traceContext: this.trace,
      clockDomain: this.clockDomain(),
      snapshots: this.snapshots,
      deviceEvents: this.deviceEvents,
      coverage: this.buildCoverage(),
    };
    if (this.captureVersion === CONTINUOUS_CAPTURE_VERSION) {
      // A continuous drain lands at a monotonic sequence under the same stable
      // session and clock-domain id, so the server can accumulate the call.
      this.drainSequence += 1;
      payload.drainSequence = this.drainSequence;
      payload.capturerStartedAtMs = this.capturerStartedAtMs;
      if (options.end !== undefined) {
        // The observed end coordinate is a real reading of the recorder's clock,
        // in the same domain as every snapshot — never fabricated, never a server
        // timestamp. Only `call_ended` finalizes; the server keeps the rest
        // provisional.
        payload.end = { reason: options.end, timestampMs: this.clock() };
      }
    }
    this.snapshots = [];
    this.deviceEvents = [];
    this.counters = zeroCounters();
    this.availability = zeroAvailability();
    this.pendingCoverage = [];
    return payload;
  }

  /** Flush a final drain with `call_ended`, then stop sampling. This explicit
   * application signal closes a version 2 call; `stop()` and page lifecycle
   * flushes do not. Version 1 simply drains and stops. Returns the payload to POST.
   */
  endCall(): CapturePayload {
    const payload = this.drain({ end: "call_ended" });
    this.stop();
    return payload;
  }

  /** Idempotently stop sampling and remove listeners. This does not close the
   * call; use `endCall()` to declare an observed application close.
   */
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
    if (counters.renderTimingUnpopulated > 0) {
      // `getOutputTimestamp()` exists but has not yet reported a playout
      // position (no audio has flowed). Unknown depth, not a zero-depth queue.
      coverage.push({
        signal: "audio.render_timing",
        availability: "partial",
        reason: "output_timestamp_unpopulated",
        droppedCount: counters.renderTimingUnpopulated,
      });
    }
    if (counters.droppedCoverageNotes > 0) {
      coverage.push({
        signal: "capture.coverage",
        availability: "partial",
        reason: "coverage_buffer_overflow",
        droppedCount: counters.droppedCoverageNotes,
      });
    }
    coverage.push(...this.platformCoverage());
    coverage.push(...this.pendingCoverage);
    return coverage;
  }

  /**
   * Coverage for render-path signals this platform does not expose.
   *
   * Each note below is a statement about the API surface, not a guess about the
   * session: without them, a browser that simply lacks a counter is
   * indistinguishable from a session in which nothing went wrong.
   */
  private platformCoverage(): CaptureCoverage[] {
    const coverage: CaptureCoverage[] = [];
    if (this.availability.renderTimingMissing) {
      coverage.push({
        signal: "audio.render_timing",
        availability: "not_observed",
        reason: "getoutputtimestamp_unavailable",
      });
    }
    if (this.availability.audioInbound) {
      // W3C webrtc-stats exposes `totalDecodeTime`/`framesDecoded` for VIDEO
      // only, so per-frame audio decode time is not measurable here at all. The
      // nearest governed signal is `totalProcessingDelay` (received -> decoded).
      coverage.push({
        signal: "webrtc.audio_decode_time",
        availability: "not_observed",
        reason: "decode_time_is_video_only_in_w3c_stats",
      });
      if (!this.availability.processingDelay) {
        coverage.push({
          signal: "webrtc.processing_delay",
          availability: "not_observed",
          reason: "member_not_exposed",
        });
      }
    }
    if (this.availability.sampledStats && !this.availability.playout) {
      coverage.push({
        signal: "webrtc.playout",
        availability: "not_observed",
        reason: "media_playout_stat_not_exposed",
      });
    }
    return coverage;
  }

  /** Note which render-path stats this platform actually produced. */
  private noteStatsAvailability(snapshot: WebRtcSnapshot): void {
    this.availability.sampledStats = true;
    for (const stat of Object.values(snapshot.stats)) {
      if (stat.type === "media-playout") {
        this.availability.playout = true;
        continue;
      }
      if (stat.type !== "inbound-rtp") continue;
      const kind = stat.kind ?? stat.mediaType;
      if (kind !== undefined && kind !== "audio") continue;
      this.availability.audioInbound = true;
      if (typeof stat.totalProcessingDelay === "number") {
        this.availability.processingDelay = true;
      }
    }
  }

  /** The render queue's depth right now, or `undefined` when unobservable. */
  private readRenderQueue(ctx: AudioContextLike): number | undefined {
    if (typeof ctx.getOutputTimestamp !== "function") return undefined;
    try {
      return renderQueueSeconds(ctx.currentTime, ctx.getOutputTimestamp());
    } catch {
      return undefined;
    }
  }

  /**
   * Sample the render position periodically. A context that cannot report one
   * gets no interval at all — just the coverage note saying so.
   */
  private scheduleRenderTiming(ctx: AudioContextLike, intervalMs: number): void {
    if (typeof ctx.getOutputTimestamp !== "function") {
      this.availability.renderTimingMissing = true;
      return;
    }
    const sample = (): void => {
      if (this.stopped) return;
      const queued = this.readRenderQueue(ctx);
      if (queued === undefined) {
        this.counters.renderTimingUnpopulated += 1;
        return;
      }
      const event = latencyEvent(undefined, ctx.outputLatency, this.clock(), queued);
      if (event) this.pushDeviceEvent(event);
    };
    const handle = this.scheduler.setInterval(sample, intervalMs);
    this.teardowns.push(() => this.scheduler.clearInterval(handle));
  }

  /**
   * Compare the capture track's settled rate with the graph's and record a
   * mismatch. Both numbers are platform-reported (`MediaTrackSettings.sampleRate`
   * and `AudioContext.sampleRate`); when either is absent nothing is claimed.
   */
  private checkSampleRate(track: MediaTrackLike): void {
    const contextHz = this.audioContext?.sampleRate;
    const trackHz = track.getSettings?.().sampleRate;
    if (typeof contextHz !== "number" || !Number.isFinite(contextHz)) return;
    if (typeof trackHz !== "number" || !Number.isFinite(trackHz)) return;
    if (contextHz === trackHz) return;
    this.pushDeviceEvent(sampleRateMismatchEvent(contextHz, trackHz, this.clock()));
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
