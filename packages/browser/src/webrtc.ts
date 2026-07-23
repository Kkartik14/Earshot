/**
 * Normalise an `RTCStatsReport` into the server snapshot shape.
 *
 * The output deserialises directly into `analyze_webrtc_stats` in
 * `packages/sdk-python/src/earshot/engines/webrtc.py`, which reads members such
 * as `type`, `packetsReceived`, `packetsLost`, `jitter`, `jitterBufferDelay`,
 * `jitterBufferEmittedCount`, `concealedSamples`, `totalSamplesReceived`,
 * `roundTripTime`/`currentRoundTripTime`, `iceState`/`dtlsState`,
 * `selectedCandidatePairId`, `selected`/`nominated`, `localCandidateId` and
 * `networkType`.
 *
 * Two disciplines the server depends on:
 *  - A member that is ABSENT on the source stat is left absent — never `0`.
 *  - We only carry JSON primitives (number/string/boolean); anything else is
 *    dropped, keeping the payload metadata-only and trivially serialisable.
 *
 * Privacy: ICE candidate stats carry raw network addresses (IP, port, URL).
 * The server engine never reads them — it reads `networkType` and the candidate
 * `id` only — so we scrub them here. No audio is ever touched (getStats never
 * exposes samples in the first place).
 */

import type { RTCStatsReportLike, StatMembers, WebRtcSnapshot } from "./types.js";

/** Network-identifying members we strip from candidate stats before emitting. */
const ADDRESS_DENYLIST = new Set<string>([
  "address",
  "ip",
  "ipAddress",
  "relatedAddress",
  "port",
  "relatedPort",
  "url",
  "hostCandidateAddress",
]);

/** Copy the JSON-primitive members of one stat, omitting scrubbed/absent ones. */
function normalizeStat(stat: Record<string, unknown>): StatMembers {
  const members: StatMembers = {};
  for (const key of Object.keys(stat)) {
    if (ADDRESS_DENYLIST.has(key)) continue; // privacy: drop network addresses
    const value = stat[key];
    if (
      typeof value === "number" ||
      typeof value === "string" ||
      typeof value === "boolean"
    ) {
      members[key] = value;
    }
    // Non-primitives (objects/arrays) are intentionally skipped: they are not
    // part of the members the server reads and keep the payload metadata-only.
  }
  return members;
}

/**
 * Turn one `RTCStatsReport` (a Map-like) plus a capture timestamp into a
 * `{ timestamp_ms, stats }` snapshot. The stat's own `id` member is preferred
 * as the map key (mirroring how `getStats()` keys the report); we fall back to
 * the report key when a member `id` is absent.
 */
export function normalizeStatsReport(
  report: RTCStatsReportLike,
  timestampMs: number,
): WebRtcSnapshot {
  const stats: Record<string, StatMembers> = {};
  report.forEach((value, key) => {
    if (value === null || typeof value !== "object") return;
    const idMember = (value as { id?: unknown }).id;
    const id = typeof idMember === "string" && idMember.length > 0 ? idMember : key;
    stats[id] = normalizeStat(value as Record<string, unknown>);
  });
  return { timestamp_ms: timestampMs, stats };
}
