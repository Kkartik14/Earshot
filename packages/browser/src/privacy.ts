/**
 * Privacy primitives for the capture kernel.
 *
 * Device labels and device/group ids are host-identifying strings (e.g.
 * "Jabra Elite 75t", a stable `deviceId`). They MUST NOT leave the client. We
 * replace each with an opaque, session-salted hash so the server can still tell
 * "same device" from "different device" within a session without ever learning
 * the name. The salt is random per recorder, so the hashes are not linkable
 * across sessions and are not a stable fingerprint.
 *
 * This is deliberately a fast, synchronous, NON-cryptographic hash (FNV-1a): it
 * runs inside device event handlers, needs no async/Web-Crypto, and its only job
 * is to be a stable opaque id. It is not a secret and must not be treated as one.
 */

import type { RandomSource } from "./types.js";

const FNV_OFFSET = 0x811c9dc5;
const FNV_PRIME = 0x01000193;

/** FNV-1a over a UTF-16 code-unit stream, returned as 8 lower-case hex chars. */
function fnv1a(input: string): string {
  let hash = FNV_OFFSET;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    // `Math.imul` keeps the multiply in 32-bit space (no BigInt needed).
    hash = Math.imul(hash, FNV_PRIME);
  }
  // >>> 0 -> unsigned 32-bit, then fixed-width hex.
  return (hash >>> 0).toString(16).padStart(8, "0");
}

/** Generate a random hex salt for one session (default 8 bytes / 16 hex). */
export function makeSalt(random: RandomSource, byteLength = 8): string {
  const bytes = new Uint8Array(byteLength);
  random(bytes);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) {
    out += (bytes[i] ?? 0).toString(16).padStart(2, "0");
  }
  return out;
}

/**
 * Hash a device label/id to an opaque, salted id like `dev_1a2b3c4d`. Empty or
 * missing input returns `undefined` so callers omit the field rather than emit a
 * hash of the empty string.
 */
export function opaqueDeviceId(
  raw: string | undefined | null,
  salt: string,
  prefix = "dev",
): string | undefined {
  if (typeof raw !== "string" || raw.length === 0) return undefined;
  return `${prefix}_${fnv1a(`${salt}:${raw}`)}`;
}
