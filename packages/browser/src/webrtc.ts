/**
 * Normalise an `RTCStatsReport` into the server snapshot shape.
 *
 * The output deserialises directly into `analyze_webrtc_stats` in
 * `packages/sdk-python/src/earshot/engines/webrtc.py`, which reads members such
 * as `type`, `packetsReceived`, `packetsLost`, `jitter`, `jitterBufferDelay`,
 * `jitterBufferEmittedCount`, `concealedSamples`, `totalSamplesReceived`,
 * `roundTripTime`/`currentRoundTripTime`, `iceState`/`dtlsState`,
 * `selectedCandidatePairId`, `selected`/`nominated`, `localCandidateId`,
 * `networkType`, `totalProcessingDelay` and the `media-playout`
 * (`RTCAudioPlayoutStats`) render counters.
 *
 * Privacy posture — allowlist, not denylist. An `RTCStatsReport` is a firehose of
 * host-identifying material: ICE candidates carry raw `address`/`ip`/`port`/`url`
 * and a `usernameFragment`; certificate stats carry a `base64Certificate` and a
 * DTLS `fingerprint`. A denylist that only removes a handful of address fields
 * lets everything it did not think to name survive. Instead we copy ONLY the
 * exact members each governed stat type needs (the fields the Python engine
 * reads), and we drop every other member — and every stat type we do not
 * consume — entirely. Nothing host-identifying can leak by omission.
 *
 * Two further disciplines the server depends on:
 *  - A member that is ABSENT on the source stat is left absent — never `0`.
 *  - We only carry JSON primitives (number/string/boolean); every retained
 *    string is length-bounded, keeping the payload metadata-only, small and
 *    trivially serialisable. No audio is ever touched (getStats never exposes
 *    samples in the first place).
 */

import type { RTCStatsReportLike, StatMembers, WebRtcSnapshot } from "./types.js";

/**
 * Members that are safe and meaningful on ANY stat we retain: the governed
 * identity/keying fields. `type` selects the engine code path, `id` keys the
 * snapshot, `kind`/`mediaType` disambiguate audio, `timestamp` is the stat's own
 * high-res reading. None are host-identifying.
 */
const UNIVERSAL_ALLOWLIST = new Set<string>(["type", "id", "kind", "timestamp"]);

/**
 * Per-stat-type allowlists: the EXACT members `analyze_webrtc_stats` reads from
 * each governed stat type. A stat type absent from this map is not consumed by
 * the server and is dropped whole (certificates, codecs, data-channels,
 * peer-connection, media-source, remote-candidate, outbound-rtp, …). Any member
 * of a retained stat that is not listed here (or universal) is dropped whole —
 * so `base64Certificate`, `fingerprint`, `usernameFragment`, `address`, `ip`,
 * `port`, `relatedAddress`, `url`, `candidateType`, device labels and every
 * other unconsumed field can never survive.
 */
const STAT_ALLOWLIST: Record<string, Set<string>> = {
  "inbound-rtp": new Set([
    "mediaType",
    "packetsReceived",
    "packetsLost",
    "packetsDiscarded",
    "fecPacketsReceived",
    "jitter",
    // Jitter-buffer depth and behaviour: the receive queue between the network
    // and the decoder. `jitterBufferDelay/EmittedCount` is the average wait a
    // sample took; target/minimum are what the buffer was aiming for; a flush is
    // the buffer discarding its contents outright.
    "jitterBufferDelay",
    "jitterBufferEmittedCount",
    "jitterBufferTargetDelay",
    "jitterBufferMinimumDelay",
    "jitterBufferFlushes",
    // Concealment and rate adaptation: what the decoder had to invent or stretch
    // when packets did not arrive in time.
    "concealedSamples",
    "silentConcealedSamples",
    "concealmentEvents",
    "insertedSamplesForDeceleration",
    "removedSamplesForAcceleration",
    "totalSamplesReceived",
    // Packet-received-to-decoded time, summed per sample. This is the closest the
    // W3C stats API comes to audio decode timing: `totalDecodeTime`/`framesDecoded`
    // are video-only, so per-frame audio decode time is genuinely not exposed and
    // the recorder records that as coverage instead of inventing it.
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
  /**
   * `RTCAudioPlayoutStats` — the render end of the path, measured at the audio
   * output device rather than inferred from a transport counter.
   * `totalPlayoutDelay/totalSamplesCount` is the average delay a played-out
   * sample carried; `synthesizedSamplesDuration` grows only when the device had
   * to invent audio because the render queue ran dry (a real under-run).
   */
  "media-playout": new Set([
    "synthesizedSamplesDuration",
    "synthesizedSamplesEvents",
    "totalSamplesDuration",
    "totalPlayoutDelay",
    "totalSamplesCount",
  ]),
};

/**
 * Upper bound on any retained string member. The governed fields are short enums
 * (`wifi`, `connected`) and opaque stat ids; a longer value is anomalous, so we
 * truncate defensively rather than forward an unbounded string off the client.
 */
const MAX_STRING_LENGTH = 128;

/** The stat's `type`, as a plain string, or `undefined` when absent/non-string. */
function statType(stat: Record<string, unknown>): string | undefined {
  const value = stat.type;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Copy ONLY the allowlisted, JSON-primitive members of one stat. Returns
 * `undefined` when the stat's `type` is one the server does not consume, so the
 * whole stat is dropped.
 */
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

/**
 * Turn one `RTCStatsReport` (a Map-like) plus a capture timestamp into a
 * `{ timestamp_ms, stats }` snapshot. The stat's own `id` member is preferred
 * as the map key (mirroring how `getStats()` keys the report); we fall back to
 * the report key when a member `id` is absent. Stats of a type the server does
 * not consume are dropped entirely.
 */
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
