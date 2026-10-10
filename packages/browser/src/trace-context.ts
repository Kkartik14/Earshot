/** W3C Trace Context helpers for joining or minting a recorder's trace. The ids
 * are random correlation handles and carry no session data or secrets.
 */

import type { RandomSource, TraceContext } from "./types.js";

const VERSION = "00";
const FLAGS_SAMPLED = "01";
const TRACE_ID_BYTES = 16;
const SPAN_ID_BYTES = 8;

function randomHex(random: RandomSource, byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  random(bytes);
  // A trace-/parent-id of all zeroes is invalid; nudge one byte if we drew it.
  if (bytes.every((b) => b === 0)) bytes[0] = 1;
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) {
    out += (bytes[i] ?? 0).toString(16).padStart(2, "0");
  }
  return out;
}

/** Mint a fresh, spec-valid, sampled trace-context. */
export function createTraceContext(random: RandomSource): TraceContext {
  const traceId = randomHex(random, TRACE_ID_BYTES);
  const spanId = randomHex(random, SPAN_ID_BYTES);
  return {
    traceId,
    spanId,
    traceparent: `${VERSION}-${traceId}-${spanId}-${FLAGS_SAMPLED}`,
  };
}

/** `version(2)-traceid(32)-spanid(16)-flags(2)`, all lower-case hex. */
const TRACEPARENT_RE = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

/** Parse a valid W3C `traceparent`, or return `null` so callers can mint one. */
export function parseTraceParent(
  traceparent: string | undefined | null,
): TraceContext | null {
  if (typeof traceparent !== "string") return null;
  const match = TRACEPARENT_RE.exec(traceparent.trim());
  if (!match) return null;
  const [, , traceId, spanId] = match;
  // All-zero trace-/span-ids are invalid per the spec.
  if (/^0+$/.test(traceId!) || /^0+$/.test(spanId!)) return null;
  return { traceId: traceId!, spanId: spanId!, traceparent: traceparent.trim() };
}

/** Return new headers with the app's `traceparent` preserved, or add this context
 * when none was supplied. The input headers are not mutated.
 */
export function injectTraceHeaders(
  context: TraceContext,
  headers: Record<string, string> = {},
): Record<string, string> {
  const existing = headers.traceparent;
  if (typeof existing === "string" && existing.length > 0) {
    return { ...headers };
  }
  return { ...headers, traceparent: context.traceparent };
}
