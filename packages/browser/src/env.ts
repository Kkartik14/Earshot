/**
 * Default host bindings for the injectable seams (clock, scheduler, randomness).
 *
 * We reach these through a single typed view of `globalThis` rather than
 * ambient DOM/Node globals, so the package needs no `lib.dom`/`@types/node` and
 * every seam stays overridable from options — which is exactly how the tests
 * drive it deterministically.
 */

import type { Clock, RandomSource, Scheduler } from "./types.js";

interface HostGlobals {
  performance?: { now?: () => number; timeOrigin?: number };
  setInterval?: (handler: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
  crypto?: { getRandomValues?: (bytes: Uint8Array) => Uint8Array };
}

const host = globalThis as unknown as HostGlobals;

/** Monotonic `performance.now()` when present, else wall-clock `Date.now()`. */
export const defaultClock: Clock = () => {
  const now = host.performance?.now;
  return typeof now === "function" ? now.call(host.performance) : Date.now();
};

/**
 * The Unix-epoch wall time (ms) the monotonic clock's origin corresponds to
 * (`performance.timeOrigin`). Falls back to reconstructing it from
 * `Date.now() - performance.now()`, or `null` when no monotonic clock exists (so
 * only the raw reading is carried and no wall calibration is possible).
 */
export function defaultWallOriginMs(): number | null {
  const origin = host.performance?.timeOrigin;
  if (typeof origin === "number" && Number.isFinite(origin)) return origin;
  const now = host.performance?.now;
  if (typeof now === "function") return Date.now() - now.call(host.performance);
  return null;
}

/** Host `setInterval`/`clearInterval`, or inert no-ops if absent. */
export const defaultScheduler: Scheduler = {
  setInterval(handler, ms) {
    return host.setInterval ? host.setInterval(handler, ms) : undefined;
  },
  clearInterval(handle) {
    host.clearInterval?.(handle);
  },
};

/**
 * Web-Crypto randomness when available, else a `Math.random` fallback. The
 * fallback is fine here: trace/span ids and the privacy salt are correlation
 * ids, not cryptographic secrets, and carry no user data.
 */
export const defaultRandomSource: RandomSource = (bytes) => {
  const getRandomValues = host.crypto?.getRandomValues;
  if (typeof getRandomValues === "function") {
    getRandomValues.call(host.crypto, bytes);
    return;
  }
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Math.floor(Math.random() * 256);
  }
};
