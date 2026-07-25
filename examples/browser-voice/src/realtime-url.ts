/**
 * Pure helpers for the raw OpenAI Realtime WebRTC handshake — the parts that do
 * not touch the DOM or the network, so they can be unit-tested without a browser
 * and without ever calling OpenAI.
 *
 * The actual connection lives in `openai-realtime.ts`; it reads its credential
 * from the runtime UI (never from build-time env, never committed) and only then
 * performs the SDP exchange described here.
 */

/** OpenAI's Realtime SDP-exchange base endpoint. */
export const OPENAI_REALTIME_BASE_URL = "https://api.openai.com/v1/realtime";

/** A reasonable default Realtime model; overridable from the UI. */
export const DEFAULT_OPENAI_REALTIME_MODEL = "gpt-4o-realtime-preview";

export interface RealtimeUrlConfig {
  /** Realtime model id; falls back to {@link DEFAULT_OPENAI_REALTIME_MODEL}. */
  model?: string;
  /** Override the base URL (e.g. an OpenAI-compatible gateway). */
  baseUrl?: string;
}

/**
 * Build the URL the SDP offer is POSTed to: `${base}?model=${model}`. The browser
 * sends its offer SDP here with `Authorization: Bearer <runtime key>` and
 * `Content-Type: application/sdp`, and OpenAI answers with the SDP answer.
 */
export function realtimeSdpUrl(config: RealtimeUrlConfig = {}): string {
  const base = config.baseUrl?.trim() || OPENAI_REALTIME_BASE_URL;
  const model = config.model?.trim() || DEFAULT_OPENAI_REALTIME_MODEL;
  const url = new URL(base);
  url.searchParams.set("model", model);
  return url.toString();
}

/**
 * Return a trimmed runtime key or throw with guidance. The key is NEVER read
 * from build-time env or a committed constant — it is provided at runtime (a UI
 * field), so the example builds and typechecks with no key present.
 */
export function requireRealtimeKey(raw: string | null | undefined): string {
  const key = (raw ?? "").trim();
  if (key.length === 0) {
    throw new Error(
      "No OpenAI Realtime key. Paste an ephemeral client secret (recommended: " +
        "mint it server-side with POST /v1/realtime/sessions) into the key field " +
        "before connecting. The example never ships or requires a key to build.",
    );
  }
  return key;
}
