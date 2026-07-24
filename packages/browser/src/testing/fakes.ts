/**
 * Mocked W3C APIs for the capture-kernel tests.
 *
 * Because the kernel consumes only structural interfaces (see `../types.ts`),
 * these fakes are tiny hand-written stand-ins for `RTCPeerConnection`,
 * `RTCStatsReport`, `AudioContext`, `MediaDevices`, Permissions and friends —
 * no jsdom, no real browser. Each fake exposes a `dispatch`/`set*` helper so a
 * test can drive lifecycle transitions deterministically.
 *
 * Excluded from the built package (see `tsconfig.build.json`).
 */

import type { CaptureRequestInit, FetchLike } from "../transport.js";
import type {
  AudioContextLike,
  AudioTimestampLike,
  MediaDevicesLike,
  MediaStreamLike,
  MediaTrackLike,
  PeerConnectionLike,
  PermissionStatusLike,
  PermissionsLike,
  RandomSource,
  RTCStatsReportLike,
  Scheduler,
} from "../types.js";

// ---------------------------------------------------------------------------
// Deterministic seams
// ---------------------------------------------------------------------------

/** A controllable monotonic clock: each `now()` returns then advances by `step`. */
export class FakeClock {
  constructor(
    private value = 1000,
    private readonly step = 1,
  ) {}

  now = (): number => {
    const current = this.value;
    this.value += this.step;
    return current;
  };

  set(value: number): void {
    this.value = value;
  }
}

/** A `Scheduler` that never fires on its own — the test calls `fireAll`. */
export class FakeScheduler implements Scheduler {
  private readonly tasks = new Map<number, () => unknown>();
  private nextId = 1;

  setInterval(handler: () => void, _ms: number): unknown {
    const id = this.nextId;
    this.nextId += 1;
    this.tasks.set(id, handler);
    return id;
  }

  clearInterval(handle: unknown): void {
    if (typeof handle === "number") this.tasks.delete(handle);
  }

  /** Number of live intervals (0 after everything is torn down). */
  get activeCount(): number {
    return this.tasks.size;
  }

  /** The live interval handlers, for tests that drive concurrency by hand. */
  handlers(): Array<() => unknown> {
    return [...this.tasks.values()];
  }

  /** Invoke every live interval `times` times, awaiting each (async) tick. */
  async fireAll(times = 1): Promise<void> {
    for (let i = 0; i < times; i += 1) {
      for (const task of [...this.tasks.values()]) {
        await task();
      }
    }
  }
}

/** A deterministic, non-zero random source (fills bytes with a rising counter). */
export function sequentialRandom(start = 1): RandomSource {
  let counter = start;
  return (bytes: Uint8Array): void => {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = counter & 0xff;
      counter += 1;
    }
  };
}

// ---------------------------------------------------------------------------
// WebRTC fakes
// ---------------------------------------------------------------------------

/** Build a Map-backed `RTCStatsReport` from `{ id: members }`. */
export function makeStatsReport(
  entries: Record<string, Record<string, unknown>>,
): RTCStatsReportLike {
  return new Map<string, Record<string, unknown>>(Object.entries(entries));
}

/** A `RTCPeerConnection` whose `getStats()` walks a fixed list of reports. */
export class FakePeerConnection implements PeerConnectionLike {
  getStatsCalls = 0;
  private index = 0;

  constructor(private readonly reports: RTCStatsReportLike[]) {}

  getStats(): Promise<RTCStatsReportLike> {
    this.getStatsCalls += 1;
    const clamped = Math.min(this.index, this.reports.length - 1);
    this.index += 1;
    const report = this.reports[clamped];
    return report
      ? Promise.resolve(report)
      : Promise.reject(new Error("FakePeerConnection: no reports configured"));
  }
}

/**
 * A `RTCPeerConnection` whose `getStats()` stays pending until `resolveNext()`
 * is called, letting a test hold a sample in flight and prove the recorder skips
 * an overlapping one instead of running two concurrently.
 */
export class ControllablePeerConnection implements PeerConnectionLike {
  getStatsCalls = 0;
  private readonly resolvers: Array<(report: RTCStatsReportLike) => void> = [];

  constructor(private readonly report: RTCStatsReportLike) {}

  getStats(): Promise<RTCStatsReportLike> {
    this.getStatsCalls += 1;
    return new Promise<RTCStatsReportLike>((resolve) => this.resolvers.push(resolve));
  }

  /** How many getStats() promises are still awaiting a resolution. */
  get pending(): number {
    return this.resolvers.length;
  }

  /** Resolve the oldest in-flight getStats() with the fixed report. */
  resolveNext(): void {
    const resolve = this.resolvers.shift();
    if (resolve) resolve(this.report);
  }
}

// ---------------------------------------------------------------------------
// Event-target base + Web Audio / media fakes
// ---------------------------------------------------------------------------

export class FakeEventTarget {
  private readonly listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, listener: () => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(listener);
  }

  removeEventListener(type: string, listener: () => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  dispatch(type: string): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener();
  }

  /** Total live listeners (optionally for one `type`). */
  listenerCount(type?: string): number {
    if (type !== undefined) return this.listeners.get(type)?.size ?? 0;
    let total = 0;
    for (const set of this.listeners.values()) total += set.size;
    return total;
  }
}

export interface FakeAudioContextOptions {
  state?: string;
  baseLatency?: number;
  outputLatency?: number;
  sinkId?: string | { type: string };
  currentTime?: number;
  sampleRate?: number;
  /** Omit to model a browser without `getOutputTimestamp` (partial support). */
  contextTime?: number | null;
}

export class FakeAudioContext extends FakeEventTarget implements AudioContextLike {
  state: string;
  baseLatency?: number;
  outputLatency?: number;
  sinkId?: string | { type: string };
  currentTime?: number;
  sampleRate?: number;
  contextTime?: number;
  getOutputTimestamp?: () => AudioTimestampLike;

  constructor(options: FakeAudioContextOptions = {}) {
    super();
    this.state = options.state ?? "running";
    this.baseLatency = options.baseLatency;
    this.outputLatency = options.outputLatency;
    this.sinkId = options.sinkId;
    this.currentTime = options.currentTime;
    this.sampleRate = options.sampleRate;
    if (options.contextTime !== null && options.contextTime !== undefined) {
      this.contextTime = options.contextTime;
    }
    if (options.contextTime !== null) {
      // `null` models a platform that does not implement the method at all;
      // `undefined` models one that implements it but has no playout position yet.
      this.getOutputTimestamp = (): AudioTimestampLike => ({
        contextTime: this.contextTime,
        performanceTime: 0,
      });
    }
  }

  /** Advance the graph clock and the playout position independently. */
  setRenderPosition(currentTime: number, contextTime: number | undefined): void {
    this.currentTime = currentTime;
    this.contextTime = contextTime;
  }

  /** Transition state and fire `statechange` (as the real context does). */
  setState(state: string): void {
    this.state = state;
    this.dispatch("statechange");
  }

  /** Switch output sink and fire `sinkchange`. */
  setSink(sinkId: string | { type: string }): void {
    this.sinkId = sinkId;
    this.dispatch("sinkchange");
  }
}

export class FakeMediaTrack extends FakeEventTarget implements MediaTrackLike {
  readonly kind = "audio";

  constructor(
    private readonly settings: {
      deviceId?: string;
      groupId?: string;
      label?: string;
      sampleRate?: number;
    } = {},
  ) {
    super();
  }

  getSettings(): {
    deviceId?: string;
    groupId?: string;
    label?: string;
    sampleRate?: number;
  } {
    return this.settings;
  }

  /** Simulate the underlying device going away. */
  end(): void {
    this.dispatch("ended");
  }
}

export class FakeMediaStream implements MediaStreamLike {
  constructor(private readonly tracks: MediaTrackLike[]) {}

  getAudioTracks(): MediaTrackLike[] {
    return this.tracks;
  }
}

export interface FakeMediaDevicesOptions {
  stream?: MediaStreamLike;
  error?: { name: string };
}

export class FakeMediaDevices extends FakeEventTarget implements MediaDevicesLike {
  getUserMediaCalls = 0;

  constructor(private readonly options: FakeMediaDevicesOptions = {}) {
    super();
  }

  getUserMedia(_constraints?: unknown): Promise<MediaStreamLike> {
    this.getUserMediaCalls += 1;
    if (this.options.error) return Promise.reject(this.options.error);
    if (this.options.stream) return Promise.resolve(this.options.stream);
    return Promise.reject(new Error("FakeMediaDevices: nothing configured"));
  }

  /** Simulate a `devicechange`. */
  triggerDeviceChange(): void {
    this.dispatch("devicechange");
  }
}

export class FakePermissionStatus
  extends FakeEventTarget
  implements PermissionStatusLike
{
  state: string;

  constructor(state = "prompt") {
    super();
    this.state = state;
  }

  /** Transition permission state and fire `change`. */
  setState(state: string): void {
    this.state = state;
    this.dispatch("change");
  }
}

export class FakePermissions implements PermissionsLike {
  constructor(private readonly status: FakePermissionStatus) {}

  query(_descriptor: { name: string }): Promise<PermissionStatusLike> {
    return Promise.resolve(this.status);
  }
}

// ---------------------------------------------------------------------------
// Capture transport fakes
// ---------------------------------------------------------------------------

/** One recorded POST, so a test can assert on headers and body without a server. */
export interface RecordedRequest {
  url: string;
  init: CaptureRequestInit;
}

/**
 * A scripted `fetch`: each entry is either an HTTP status to answer with or
 * `"throw"` to model a transport-level failure. The last entry repeats once the
 * script runs out, so a test can drive "always failing" without a long list.
 */
export class FakeFetch {
  readonly requests: RecordedRequest[] = [];

  constructor(private readonly script: Array<number | "throw"> = [201]) {}

  get calls(): number {
    return this.requests.length;
  }

  fetch: FetchLike = (url, init) => {
    const index = Math.min(this.requests.length, this.script.length - 1);
    this.requests.push({ url, init });
    const outcome = this.script[index] ?? 201;
    if (outcome === "throw") {
      // A real fetch rejection can quote the whole request; the transport must
      // never read it, so the fake makes that mistake visible if it ever does.
      return Promise.reject(
        new Error(`network unreachable: ${url} ${JSON.stringify(init)}`),
      );
    }
    return Promise.resolve({ ok: outcome >= 200 && outcome < 300, status: outcome });
  };

  /** The bodies posted so far, parsed. */
  bodies(): Array<Record<string, unknown>> {
    return this.requests.map(
      (request) => JSON.parse(request.init.body) as Record<string, unknown>,
    );
  }
}

/** A `sleep` that records the delays it was asked for and never actually waits. */
export class FakeSleep {
  readonly delays: number[] = [];

  sleep = (ms: number): Promise<void> => {
    this.delays.push(ms);
    return Promise.resolve();
  };
}
