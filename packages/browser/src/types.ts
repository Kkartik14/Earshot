/**
 * Structural (duck-typed) shapes of the W3C APIs this capture kernel consumes,
 * plus the payload shapes it produces.
 *
 * Why structural instead of `lib.dom.d.ts`? This package targets `lib: ES2022`
 * with no DOM lib (see the repo `tsconfig.base.json`) so it type-checks and runs
 * in both a browser and Node/worker/test context. We depend only on the exact
 * members we read from each API, which also makes the whole surface trivial to
 * mock (see `src/testing/fakes.ts`). Nothing here reads audio samples — the
 * kernel is metadata-only by construction.
 */

// ---------------------------------------------------------------------------
// WebRTC — the subset of RTCPeerConnection / RTCStatsReport we touch
// ---------------------------------------------------------------------------

/**
 * A single `RTCStats`-shaped member bag. Every value is a JSON primitive; the
 * server engine reads members like `type`, `packetsReceived`, `packetsLost`,
 * `jitter`, `jitterBufferDelay`, `jitterBufferEmittedCount`, `concealedSamples`,
 * `totalSamplesReceived`, `roundTripTime`, `iceState`, `selectedCandidatePairId`,
 * `networkType`, etc. A member that was ABSENT on the source stat stays absent
 * here — it is never coerced to `0` (a W3C discipline the server relies on).
 */
export type StatMembers = Record<string, string | number | boolean>;

/**
 * The minimal `RTCStatsReport` surface. The real report is a read-only `Map`;
 * a plain `Map` satisfies this, which is how the tests build fixtures.
 */
export interface RTCStatsReportLike {
  forEach(callback: (value: Record<string, unknown>, key: string) => void): void;
}

/** The minimal `RTCPeerConnection` surface: we only ever sample `getStats()`. */
export interface PeerConnectionLike {
  getStats(): Promise<RTCStatsReportLike>;
}

/** One captured `getStats()` snapshot, exactly as `analyze_webrtc_stats` expects. */
export interface WebRtcSnapshot {
  /** Milliseconds from the injected monotonic clock at sample time. */
  timestamp_ms: number;
  /** Stat id -> `RTCStats`-shaped member bag (missing members omitted). */
  stats: Record<string, StatMembers>;
}

// ---------------------------------------------------------------------------
// Web Audio / media devices — the subset we observe
// ---------------------------------------------------------------------------

/** The minimal event-target surface (Web Audio / MediaDevices / PermissionStatus). */
export interface EventTargetLike {
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

/** The minimal `AudioContext` surface we read. */
export interface AudioContextLike extends EventTargetLike {
  /** "running" | "suspended" | "closed" | (iOS) "interrupted". */
  readonly state: string;
  /** Deterministic context property, in seconds (a `measured` latency). */
  readonly baseLatency?: number;
  /** W3C *estimate*, in seconds — surfaced but flagged as an estimate. */
  readonly outputLatency?: number;
  /** Current output sink id (a device id — hashed before it leaves the client). */
  readonly sinkId?: string | { type: string };
}

/** A single audio `MediaStreamTrack` surface (lifecycle + settings only). */
export interface MediaTrackLike extends EventTargetLike {
  readonly kind?: string;
  getSettings?(): { deviceId?: string; groupId?: string; label?: string };
  stop?(): void;
}

/** A `MediaStream` surface: we only enumerate its audio tracks. */
export interface MediaStreamLike {
  getAudioTracks(): MediaTrackLike[];
}

/** A `MediaDeviceInfo` surface (labels/ids are hashed, never emitted raw). */
export interface MediaDeviceInfoLike {
  readonly deviceId?: string;
  readonly groupId?: string;
  readonly kind?: string;
  readonly label?: string;
}

/** The minimal `navigator.mediaDevices` surface. */
export interface MediaDevicesLike extends EventTargetLike {
  getUserMedia(constraints?: unknown): Promise<MediaStreamLike>;
  enumerateDevices?(): Promise<MediaDeviceInfoLike[]>;
}

/** A `PermissionStatus` surface. */
export interface PermissionStatusLike extends EventTargetLike {
  readonly state: string;
}

/** The minimal `navigator.permissions` surface. */
export interface PermissionsLike {
  query(descriptor: { name: string }): Promise<PermissionStatusLike>;
}

/**
 * One captured device / audio-graph event, exactly as `analyze_audio_graph`
 * expects: a `{ type, timestamp_ms, ... }` mapping. Types are lower-case and
 * drawn from the vocabulary the server engine recognises (`permission`,
 * `audiocontext_state`, `devicechange`, `sink_change`, `sample_rate_mismatch`,
 * `underrun`, `latency`); any additional members are extra metadata the engine
 * ignores (it is fail-open). Device labels/ids never appear here — only opaque
 * salted hashes under `deviceHash` / `sinkHash`.
 */
export interface DeviceEvent {
  type: string;
  timestamp_ms: number;
  [member: string]: string | number | boolean | undefined;
}

// ---------------------------------------------------------------------------
// Trace context (W3C) + the drained payload
// ---------------------------------------------------------------------------

/** A W3C trace-context for the session (no secrets — random ids only). */
export interface TraceContext {
  /** `version-traceid-spanid-flags`, e.g. `00-<32hex>-<16hex>-01`. */
  traceparent: string;
  traceId: string;
  spanId: string;
}

/**
 * The browser's own clock identity, carried so the server never mistakes a
 * browser timestamp for a server-clock observation.
 *
 * Every `timestamp_ms` in this payload's snapshots/events is a RAW reading of
 * this clock — a monotonic `performance.now()` (or `Date.now()`) value, NOT
 * rebased to zero. The server records those readings as `monotonic_time_nano`
 * inside a declared `ClockDomain` whose id is `id`; because there is no
 * calibration (`ClockRelation`) to the server clock, cross-clock latency stays
 * honestly *unavailable* unless a caller supplies one. The id is stable for the
 * recorder's lifetime, so batches drained separately share one continuous
 * browser timeline instead of each restarting at zero.
 */
export interface BrowserClockDomain {
  /** Stable opaque per-recorder clock id, e.g. `clk_<16hex>`. */
  id: string;
  /** The clock family — a monotonic browser clock, not the server wall clock. */
  kind: "browser_monotonic";
  /** The unit every `timestamp_ms` in this payload is expressed in. */
  unit: "ms";
  /**
   * The clock's own reading uncertainty, in ms. Browsers coarsen
   * `performance.now()` (often to ~1ms), so this is carried forward as the
   * `uncertainty_nano` of every browser-domain fact rather than pretending the
   * readings are exact.
   */
  uncertaintyMs: number;
  /**
   * The Unix-epoch wall time (ms) the monotonic origin corresponds to
   * (`performance.timeOrigin`). Browser wall time at reading `t` is
   * `wallOriginMs + t`. The server carries this as the domain's wall origin so a
   * declared client<->server `ClockRelation` has a wall timestamp to align —
   * without one, cross-clock latency stays unavailable. `null` when the runtime
   * exposes no wall origin (then only the monotonic reading is carried and
   * calibration is impossible).
   */
  wallOriginMs: number | null;
}

/**
 * An explicit *unknown* the capture kernel recorded rather than dropping
 * silently: a bounded-buffer overflow, a `getStats()`/permission error, or an
 * overlapping sample it skipped. The server can ledger these as coverage so a
 * gap is never mistaken for a clean measurement.
 */
export interface CaptureCoverage {
  /** The fact source the gap concerns (e.g. `webrtc.snapshots`). */
  signal: string;
  /** `partial` | `not_observed` | `available`. */
  availability: string;
  /** Why the gap exists (e.g. `buffer_overflow_oldest_dropped`). */
  reason: string;
  /** How many observations were lost/skipped in this window, when countable. */
  droppedCount?: number;
}

/** The unit the client POSTs to the server, which feeds the two engines. */
export interface CapturePayload {
  sessionId: string;
  traceContext: TraceContext;
  /** The browser clock these snapshots'/events' `timestamp_ms` values belong to. */
  clockDomain: BrowserClockDomain;
  /** Ordered `getStats` snapshots -> `analyze_webrtc_stats`. */
  snapshots: WebRtcSnapshot[];
  /** Ordered device/audio events -> `analyze_audio_graph`. */
  deviceEvents: DeviceEvent[];
  /** Explicit coverage gaps recorded this window (never silent loss). */
  coverage: CaptureCoverage[];
}

// ---------------------------------------------------------------------------
// Injected environment (clock / scheduler / randomness) — the seams for tests
// ---------------------------------------------------------------------------

/** A high-resolution, monotonic time source in milliseconds. */
export type Clock = () => number;

/** The interval scheduler surface (host `setInterval` / `clearInterval`). */
export interface Scheduler {
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

/** Fills `bytes` with random data (host `crypto.getRandomValues` by default). */
export type RandomSource = (bytes: Uint8Array) => void;
