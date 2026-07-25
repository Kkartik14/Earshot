/**
 * W3C Trace Context generation for a capture session.
 *
 * We mint a single `traceparent` per recorder so the client-captured telemetry
 * can be correlated with the server-side spans it belongs to. Per the spec
 * (https://www.w3.org/TR/trace-context/#traceparent-header):
 *
 *   traceparent = version "-" trace-id "-" parent-id "-" trace-flags
 *   version   = 2 hex   (we emit "00")
 *   trace-id  = 32 hex  (16 random bytes, MUST NOT be all zero)
 *   parent-id = 16 hex  (8 random bytes,  MUST NOT be all zero)
 *   flags     = 2 hex   ("01" = sampled)
 *
 * The ids are random and carry NO user data, no session content, no secrets —
 * they are pure correlation handles.
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

/**
 * Parse a W3C `traceparent` header into a `TraceContext` so the recorder can
 * JOIN the application's existing trace instead of minting its own. Returns
 * `null` when the value is absent or not spec-valid (all-zero ids are invalid),
 * so the caller can fall back to minting a fresh context. Never throws.
 */
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

/**
 * Return a new headers object carrying a `traceparent`, merged over any provided
 * headers. If the caller already set a `traceparent` we PRESERVE it — the
 * application's own trace is never overwritten; we only fill one in when none is
 * present. Pure (does not mutate the input) so it is safe to hand a shared
 * default-headers object.
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
