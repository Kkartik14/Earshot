/**
 * Builders for the device / audio-graph event stream.
 *
 * Every event returned here deserialises into `analyze_audio_graph` in
 * `packages/sdk-python/src/earshot/engines/device.py`. That engine keys off a
 * lower-case `type` and reads a small, fixed vocabulary of members; we emit
 * exactly those. The recorder owns the listener lifecycle — these functions are
 * pure so the mapping is unit-testable in isolation.
 *
 * Server-recognised `type` values we produce:
 *  - "permission"           + { state }                  (denied -> permission_denied)
 *  - "audiocontext_state"   + { state }                  (suspended/interrupted)
 *  - "devicechange"                                       (device route changed)
 *  - "sink_change"          + { sinkHash? }               (output route changed)
 *  - "sample_rate_mismatch" + { configured_hz, actual_hz }
 *  - "underrun" / "dropped_frames"
 *  - "latency"              + { base_latency_s, output_latency_s, render_queue_s }
 *
 * Privacy: sink/device ids arrive here already hashed (`sinkHash`, `deviceHash`);
 * raw labels/ids never reach this module.
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

/**
 * A latency / render-timing reading. `baseLatency` is a deterministic context
 * property; the server labels it `measured`. `outputLatency` is a W3C
 * *estimate*; the server labels it `estimated`. `renderQueueS` is
 * `currentTime - getOutputTimestamp().contextTime` — the audio the graph has
 * already rendered but the output device has not yet played, i.e. the render
 * queue's depth — and is likewise an estimate (the two readings are taken at
 * slightly different instants). All three are in seconds (Web Audio's own unit)
 * and whichever the platform does not expose is omitted: an absent latency is
 * unknown, not zero.
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

/**
 * The render queue's depth in seconds, or `undefined` when the platform cannot
 * tell us. `getOutputTimestamp()` is optional in practice (partial support), and
 * even where it exists it returns an unpopulated timestamp until audio actually
 * flows — so a missing, non-finite or negative result is reported as *unknown*
 * (the caller records coverage), never as a zero-depth queue.
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
