/** Default injectable clock, scheduler, and randomness bindings without DOM or Node type dependencies. */

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

/** Wall-clock time at the monotonic clock's origin, or `null` when unavailable. */
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

/** Web Crypto when available; fallback values are correlation ids, not secrets. */
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
