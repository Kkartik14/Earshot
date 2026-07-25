import { describe, expect, it } from "vitest";

import {
  DEFAULT_OPENAI_REALTIME_MODEL,
  OPENAI_REALTIME_BASE_URL,
  realtimeSdpUrl,
  requireRealtimeKey,
} from "./realtime-url.js";

describe("realtimeSdpUrl", () => {
  it("defaults to the OpenAI base and default model", () => {
    expect(realtimeSdpUrl()).toBe(
      `${OPENAI_REALTIME_BASE_URL}?model=${DEFAULT_OPENAI_REALTIME_MODEL}`,
    );
  });

  it("uses the supplied model", () => {
    expect(realtimeSdpUrl({ model: "gpt-4o-realtime-preview-2024-12-17" })).toBe(
      `${OPENAI_REALTIME_BASE_URL}?model=gpt-4o-realtime-preview-2024-12-17`,
    );
  });

  it("honours a base URL override for gateways", () => {
    expect(realtimeSdpUrl({ baseUrl: "https://gateway.example/v1/realtime" })).toBe(
      `https://gateway.example/v1/realtime?model=${DEFAULT_OPENAI_REALTIME_MODEL}`,
    );
  });

  it("treats blank overrides as absent", () => {
    expect(realtimeSdpUrl({ model: "   ", baseUrl: "  " })).toBe(
      `${OPENAI_REALTIME_BASE_URL}?model=${DEFAULT_OPENAI_REALTIME_MODEL}`,
    );
  });
});

describe("requireRealtimeKey", () => {
  it("returns a trimmed key when present", () => {
    expect(requireRealtimeKey("  sk-ephemeral  ")).toBe("sk-ephemeral");
  });

  it.each([null, undefined, "", "   "])("throws when the key is %o", (value) => {
    expect(() => requireRealtimeKey(value)).toThrow(/No OpenAI Realtime key/);
  });
});
