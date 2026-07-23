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

import type {
  AudioContextLike,
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
}

export class FakeAudioContext extends FakeEventTarget implements AudioContextLike {
  state: string;
  baseLatency?: number;
  outputLatency?: number;
  sinkId?: string | { type: string };

  constructor(options: FakeAudioContextOptions = {}) {
    super();
    this.state = options.state ?? "running";
    this.baseLatency = options.baseLatency;
    this.outputLatency = options.outputLatency;
    this.sinkId = options.sinkId;
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
    } = {},
  ) {
    super();
  }

  getSettings(): { deviceId?: string; groupId?: string; label?: string } {
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
