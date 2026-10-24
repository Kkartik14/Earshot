/** Normalize RTC stats with per-type allowlists; missing members stay absent and captured data remains metadata-only. */

import type { RTCStatsReportLike, StatMembers, WebRtcSnapshot } from "./types.js";

/** Identity and timestamp members safe on every retained stat. */
const UNIVERSAL_ALLOWLIST = new Set<string>(["type", "id", "kind", "timestamp"]);

/** Exact server-consumed members by stat type; unknown types and fields are dropped. */
const STAT_ALLOWLIST: Record<string, Set<string>> = {
  "inbound-rtp": new Set([
    "mediaType",
    "packetsReceived",
    "packetsLost",
    "packetsDiscarded",
    "fecPacketsReceived",
    "jitter",
    // Jitter-buffer depth, target, and flush counts describe the receive queue.
    "jitterBufferDelay",
    "jitterBufferEmittedCount",
    "jitterBufferTargetDelay",
    "jitterBufferMinimumDelay",
    "jitterBufferFlushes",
    // Decoder concealment and sample-rate adaptation when packets arrive late.
    "concealedSamples",
    "silentConcealedSamples",
    "concealmentEvents",
    "insertedSamplesForDeceleration",
    "removedSamplesForAcceleration",
    "totalSamplesReceived",
    // Packet-to-decode delay is exposed; per-frame audio decode time is not.
    "totalProcessingDelay",
  ]),
  "remote-inbound-rtp": new Set(["roundTripTime"]),
  "candidate-pair": new Set([
    "state",
    "selected",
    "nominated",
    "localCandidateId",
    "candidatePairId",
    "currentRoundTripTime",
    "roundTripTime",
  ]),
  transport: new Set([
    "iceState",
    "dtlsState",
    "connectionState",
    "selectedCandidatePairId",
  ]),
  "local-candidate": new Set(["networkType"]),
  /** Render-path measurements reported by the audio output device. */
  "media-playout": new Set([
    "synthesizedSamplesDuration",
    "synthesizedSamplesEvents",
    "totalSamplesDuration",
    "totalPlayoutDelay",
    "totalSamplesCount",
  ]),
};

/** Bound retained strings before they leave the client. */
const MAX_STRING_LENGTH = 128;

/** The stat's `type`, as a plain string, or `undefined` when absent/non-string. */
function statType(stat: Record<string, unknown>): string | undefined {
  const value = stat.type;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/** Copy allowlisted primitive members, or drop stat types the server does not consume. */
function normalizeStat(stat: Record<string, unknown>): StatMembers | undefined {
  const type = statType(stat);
  if (type === undefined) return undefined; // untyped stat: not consumed, drop whole
  const allowed = STAT_ALLOWLIST[type];
  if (allowed === undefined) return undefined; // unconsumed stat type: drop whole

  const members: StatMembers = {};
  for (const key of Object.keys(stat)) {
    if (!UNIVERSAL_ALLOWLIST.has(key) && !allowed.has(key)) continue; // drop anything not governed
    const value = stat[key];
    if (typeof value === "number" || typeof value === "boolean") {
      members[key] = value;
    } else if (typeof value === "string") {
      members[key] =
        value.length > MAX_STRING_LENGTH ? value.slice(0, MAX_STRING_LENGTH) : value;
    }
    // Non-primitives (objects/arrays) are intentionally skipped.
  }
  return members;
}

/** Convert an RTC stats report to a timestamped snapshot, dropping unconsumed types. */
export function normalizeStatsReport(
  report: RTCStatsReportLike,
  timestampMs: number,
): WebRtcSnapshot {
  const stats: Record<string, StatMembers> = {};
  report.forEach((value, key) => {
    if (value === null || typeof value !== "object") return;
    const members = normalizeStat(value as Record<string, unknown>);
    if (members === undefined) return; // unconsumed stat type: not retained
    const idMember = (value as { id?: unknown }).id;
    const id = typeof idMember === "string" && idMember.length > 0 ? idMember : key;
    stats[id] = members;
  });
  return { timestamp_ms: timestampMs, stats };
}
