/** Capture payload version, carried in the body so its shape can evolve without
 * changing the `/v1` route. Bump it when the current server cannot read the shape.
 */
export const CAPTURE_PROTOCOL_VERSION = 1;

/** Opt-in version that accumulates sequenced drains into one server-side call. */
export const CONTINUOUS_CAPTURE_VERSION = 2;

/** The capture wire versions this client can emit. */
export type CaptureVersion =
  typeof CAPTURE_PROTOCOL_VERSION | typeof CONTINUOUS_CAPTURE_VERSION;
