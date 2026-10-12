/** Pure URL and runtime-key helpers for the OpenAI Realtime SDP exchange. */

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

/** Build the SDP exchange URL for the selected model. */
export function realtimeSdpUrl(config: RealtimeUrlConfig = {}): string {
  const base = config.baseUrl?.trim() || OPENAI_REALTIME_BASE_URL;
  const model = config.model?.trim() || DEFAULT_OPENAI_REALTIME_MODEL;
  const url = new URL(base);
  url.searchParams.set("model", model);
  return url.toString();
}

/** Return a trimmed runtime key or throw if none was supplied. */
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
