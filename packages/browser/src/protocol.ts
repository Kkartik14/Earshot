/**
 * The capture wire-format version.
 *
 * It travels in the payload body (`CapturePayload.captureVersion`), not in the
 * URL, so the client and the server can evolve the shape independently of the
 * `/v1` route they share with every other endpoint. The server accepts the
 * versions it governs and answers anything else with a specific, clean client
 * error (`EARSHOT_UNSUPPORTED_CAPTURE_VERSION`) rather than a pile of schema
 * complaints about a format the client was never targeting.
 *
 * Bump this only when the payload shape changes in a way the current server
 * cannot read; the server side of the contract lives in
 * `packages/sdk-python/src/earshot/api.py` (`CAPTURE_PROTOCOL_VERSION`).
 */
export const CAPTURE_PROTOCOL_VERSION = 1;

/**
 * The continuous-capture wire version. A recorder created with
 * `{ captureVersion: 2 }` accumulates one call into a single journal-backed
 * provisional artifact on the server instead of a per-drain incident: each
 * `drain()` carries a 1-based `drainSequence` under the same continuous
 * `sessionId` and `clockDomain.id`. It stays opt-in for now so a client can keep
 * talking to a server that only governs version 1; the server governs both.
 */
export const CONTINUOUS_CAPTURE_VERSION = 2;

/** The capture wire versions this client can emit. */
export type CaptureVersion =
  typeof CAPTURE_PROTOCOL_VERSION | typeof CONTINUOUS_CAPTURE_VERSION;
