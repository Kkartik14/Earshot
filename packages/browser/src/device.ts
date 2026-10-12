/** Pure builders for the device-event vocabulary consumed by the server engine.
 * The recorder owns listeners; device identifiers must be hashed before entry.
 */

import type { DeviceEvent } from "./types.js";

/** Error `name`s that mean the user/agent refused microphone access. */
const PERMISSION_DENIED_ERRORS = new Set<string>([
  "NotAllowedError",
  "SecurityError",
  "PermissionDeniedError",
]);

/** A microphone permission outcome (`granted` | `denied` | `prompt`). */
export function permissionEvent(state: string, timestampMs: number): DeviceEvent {
  return { type: "permission", timestamp_ms: timestampMs, state };
}

/** An `AudioContext` state change (running | suspended | interrupted | closed). */
export function audioContextStateEvent(state: string, timestampMs: number): DeviceEvent {
  return { type: "audiocontext_state", timestamp_ms: timestampMs, state };
}

/** Latency readings in seconds. `baseLatency` is measured; `outputLatency` and
 * render-queue depth are estimates. Unavailable values are omitted, not zero.
 */
export function latencyEvent(
  baseLatencyS: number | undefined,
  outputLatencyS: number | undefined,
  timestampMs: number,
  renderQueueS?: number,
): DeviceEvent | null {
  const hasBase = typeof baseLatencyS === "number" && Number.isFinite(baseLatencyS);
  const hasOutput = typeof outputLatencyS === "number" && Number.isFinite(outputLatencyS);
  const hasQueue = typeof renderQueueS === "number" && Number.isFinite(renderQueueS);
  if (!hasBase && !hasOutput && !hasQueue) return null;
  const event: DeviceEvent = { type: "latency", timestamp_ms: timestampMs };
  if (hasBase) event.base_latency_s = baseLatencyS;
  if (hasOutput) event.output_latency_s = outputLatencyS; // estimate (see docstring)
  if (hasQueue) event.render_queue_s = renderQueueS; // estimate (see docstring)
  return event;
}

/** Compute render-queue depth in seconds, or return `undefined` when the platform
 * has no valid non-negative timestamp to report.
 */
export function renderQueueSeconds(
  currentTimeS: number | undefined,
  timestamp: { contextTime?: number } | undefined,
): number | undefined {
  const contextTime = timestamp?.contextTime;
  if (typeof currentTimeS !== "number" || !Number.isFinite(currentTimeS))
    return undefined;
  if (typeof contextTime !== "number" || !Number.isFinite(contextTime)) return undefined;
  const queued = currentTimeS - contextTime;
  return Number.isFinite(queued) && queued >= 0 ? queued : undefined;
}

/** A `devicechange` (an input/output device was added, removed or switched). */
export function deviceChangeEvent(timestampMs: number, deviceHash?: string): DeviceEvent {
  const event: DeviceEvent = { type: "devicechange", timestamp_ms: timestampMs };
  if (deviceHash) event.deviceHash = deviceHash;
  return event;
}

/** An output-sink change (the AudioContext started rendering to a new device). */
export function sinkChangeEvent(timestampMs: number, sinkHash?: string): DeviceEvent {
  const event: DeviceEvent = { type: "sink_change", timestamp_ms: timestampMs };
  if (sinkHash) event.sinkHash = sinkHash;
  return event;
}

/** A configured-vs-actual sample-rate mismatch (a stale-render signal). */
export function sampleRateMismatchEvent(
  configuredHz: number,
  actualHz: number,
  timestampMs: number,
): DeviceEvent {
  return {
    type: "sample_rate_mismatch",
    timestamp_ms: timestampMs,
    configured_hz: configuredHz,
    actual_hz: actualHz,
  };
}

/** A render buffer under-run / glitch / dropped-frame event. */
export function underrunEvent(
  timestampMs: number,
  kind: "underrun" | "dropped_frames" | "glitch" = "underrun",
): DeviceEvent {
  return { type: kind, timestamp_ms: timestampMs };
}

/** Map a `getUserMedia`/`AudioContext` rejection to `"denied"` or `null`. */
export function classifyPermissionError(error: unknown): "denied" | null {
  if (typeof error === "object" && error !== null && "name" in error) {
    const name = (error as { name?: unknown }).name;
    if (typeof name === "string" && PERMISSION_DENIED_ERRORS.has(name)) {
      return "denied";
    }
  }
  return null;
}

/** Normalise an `AudioContext.sinkId` (string, or `{ type }` for the default). */
export function sinkIdToString(
  sinkId: string | { type: string } | undefined,
): string | undefined {
  if (typeof sinkId === "string") return sinkId.length > 0 ? sinkId : undefined;
  if (sinkId && typeof sinkId === "object" && typeof sinkId.type === "string") {
    return `type:${sinkId.type}`;
  }
  return undefined;
}
