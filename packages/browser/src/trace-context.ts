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

/**
 * Return a new headers object with `traceparent` set, merged over any provided
 * headers. Pure (does not mutate the input) so it is safe to hand a shared
 * default-headers object.
 */
export function injectTraceHeaders(
  context: TraceContext,
  headers: Record<string, string> = {},
): Record<string, string> {
  return { ...headers, traceparent: context.traceparent };
}
