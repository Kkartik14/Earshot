/**
 * `EarshotBrowserRecorder` — the client-side capture kernel.
 *
 * It observes the live W3C APIs (`RTCPeerConnection.getStats()`, `AudioContext`
 * state/latency/render position, `navigator.mediaDevices` + Permissions) and
 * buffers the results in the EXACT shapes the server engines consume, then
 * `drain()`s a `CapturePayload` the client POSTs (see `transport.ts`). The
 * server feeds `snapshots` to `analyze_webrtc_stats` and `deviceEvents` to
 * `analyze_audio_graph`.
 *
 * Coverage over completeness: every render-path signal the running browser does
 * not expose — `getOutputTimestamp`, the `media-playout` stats,
 * `totalProcessingDelay` — becomes an explicit coverage note. Nothing is
 * synthesised to fill the hole, and per-frame audio decode time is never
 * claimed at all because webrtc-stats only defines it for video.
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
import { CAPTURE_PROTOCOL_VERSION } from "./protocol.js";
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

  private snapshots: WebRtcSnapshot[] = [];
  private deviceEvents: DeviceEvent[] = [];
  private counters: CoverageCounters = zeroCounters();
  private availability: SignalAvailability = zeroAvailability();
  private pendingCoverage: CaptureCoverage[] = [];
  private audioContext: AudioContextLike | null = null;
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

  // -- Web Audio -------------------------------------------------------------

  /**
   * Observe an `AudioContext` across the render path.
   *
   * Recorded on attach and then continuously:
   *  - `baseLatency` (deterministic, `measured`) and `outputLatency` (a W3C
   *    estimate) — the server keeps that distinction;
   *  - the render queue's depth, sampled periodically from
   *    `currentTime - getOutputTimestamp().contextTime` — the audio the graph
   *    has rendered that the output device has not yet played;
   *  - every `statechange` (a suspended/interrupted context is silence) and
   *    `sinkchange` (the render device moved).
   *
   * Nothing here is derived from a signal the platform does not offer: a context
   * without `getOutputTimestamp`, or one whose timestamp is not yet populated,
   * yields an explicit coverage note instead of a fabricated queue depth.
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
      if (primary) this.checkSampleRate(primary);
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

  /**
   * Ledger a gap someone else observed — most importantly the capture transport,
   * which records the observations a payload it could not deliver took with it.
   * The note joins the next `drain()`'s `coverage`, so a delivery failure is
   * still visible as an explicit unknown rather than as clean-looking silence.
   *
   * Repeat notes with the same signal/availability/reason are merged (their
   * `droppedCount`s add up) and the buffer is bounded, so an endlessly failing
   * uploader cannot grow it without limit; overflow is itself recorded.
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

  // -- drain / stop ----------------------------------------------------------

  /**
   * Hand the buffered payload to the caller and reset the buffers, so the next
   * POST starts clean. The session id, trace-context and clock-domain id are
   * stable across drains (so the browser timeline is continuous, not restarted),
   * while the per-window coverage counters reset each drain.
   */
  drain(): CapturePayload {
    const payload: CapturePayload = {
      captureVersion: CAPTURE_PROTOCOL_VERSION,
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
    this.availability = zeroAvailability();
    this.pendingCoverage = [];
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
