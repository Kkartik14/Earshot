/** Hash identifying device metadata with a per-recorder salt before it leaves the client.
 * FNV-1a is synchronous and non-cryptographic; these ids are not secrets.
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

/** Return a salted opaque device id, or `undefined` for an empty label. */
export function opaqueDeviceId(
  raw: string | undefined | null,
  salt: string,
  prefix = "dev",
): string | undefined {
  if (typeof raw !== "string" || raw.length === 0) return undefined;
  return `${prefix}_${fnv1a(`${salt}:${raw}`)}`;
}
