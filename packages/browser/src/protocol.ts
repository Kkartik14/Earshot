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
