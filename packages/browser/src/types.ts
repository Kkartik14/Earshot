/** Structural W3C API shapes and capture payload types. Keeping only the members
 * this package reads allows the same types to run in browsers, workers, and Node
 * tests without DOM library declarations. Capture is metadata-only.
 */

/** JSON-primitive RTC stats; absent source members stay absent and are never coerced to zero. */
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

/** The minimal event-target surface (Web Audio / MediaDevices / PermissionStatus). */
export interface EventTargetLike {
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

/** Audio graph and performance clock readings for the frame being rendered. */
export interface AudioTimestampLike {
  readonly contextTime?: number;
  readonly performanceTime?: number;
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
  /** Audio graph time after the latest processed block; its gap from output time estimates render-queue depth. */
  readonly currentTime?: number;
  /** The graph's sample rate in Hz — compared against the capture track's. */
  readonly sampleRate?: number;
  /** Present on browsers that expose the playout position (see above). */
  getOutputTimestamp?(): AudioTimestampLike;
}

/** A single audio `MediaStreamTrack` surface (lifecycle + settings only). */
export interface MediaTrackLike extends EventTargetLike {
  readonly kind?: string;
  getSettings?(): {
    deviceId?: string;
    groupId?: string;
    label?: string;
    /** The rate the capture device actually settled on, in Hz. */
    sampleRate?: number;
  };
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

/** Device and audio-graph event; device identifiers are sent only as opaque salted hashes. */
export interface DeviceEvent {
  type: string;
  timestamp_ms: number;
  [member: string]: string | number | boolean | undefined;
}

// Trace context (W3C) + the drained payload

/** A W3C trace-context for the session (no secrets — random ids only). */
export interface TraceContext {
  /** `version-traceid-spanid-flags`, e.g. `00-<32hex>-<16hex>-01`. */
  traceparent: string;
  traceId: string;
  spanId: string;
}

/** Browser clock identity; raw timestamps share one stable domain and are never treated as server-clock readings. */
export interface BrowserClockDomain {
  /** Stable opaque per-recorder clock id, e.g. `clk_<16hex>`. */
  id: string;
  /** The clock family — a monotonic browser clock, not the server wall clock. */
  kind: "browser_monotonic";
  /** The unit every `timestamp_ms` in this payload is expressed in. */
  unit: "ms";
  /** Clock reading uncertainty in milliseconds, propagated to browser-domain facts. */
  uncertaintyMs: number;
  /** Wall-clock time at the monotonic origin, or `null` when calibration is unavailable. */
  wallOriginMs: number | null;
}

/** Explicitly records missing observations so the server does not treat a gap as a clean measurement. */
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

/** `call_ended` finalizes the call; lifecycle reasons stop observation but leave it provisional. */
export type CaptureEndReason =
  "call_ended" | "capture_stopped" | "page_hidden" | "page_unloaded";

/** Version 2 final-drain declaration, timestamped in the recorder's clock domain. */
export interface CaptureEnd {
  reason: CaptureEndReason;
  timestampMs: number;
}

/** A declared gap between continuous (`captureVersion: 2`) drains. */
export interface CaptureResync {
  missedFromSequence: number;
  missedThroughSequence: number;
  reason: string;
}

/** The unit the client POSTs to the server, which feeds the two engines. */
export interface CapturePayload {
  /** Wire-format version, independent of the `/v1` route. */
  captureVersion: number;
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
  /** Version 2 sequence number for ordered, idempotent drains; absent in version 1. */
  drainSequence?: number;
  /** Version 2 recorder start time; this bounds capture, not the observed call duration. */
  capturerStartedAtMs?: number;
  /** `captureVersion: 2` only. A transport-declared range of lost drains. */
  resync?: CaptureResync;
  /** Version 2 final-drain reason; only `endCall()` declares `call_ended`, while `stop()` leaves the call open. */
  end?: CaptureEnd;
}

// Injected environment (clock / scheduler / randomness) — the seams for tests

/** A high-resolution, monotonic time source in milliseconds. */
export type Clock = () => number;

/** The interval scheduler surface (host `setInterval` / `clearInterval`). */
export interface Scheduler {
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

/** Fills `bytes` with random data (host `crypto.getRandomValues` by default). */
export type RandomSource = (bytes: Uint8Array) => void;
