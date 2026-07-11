import { describe, it, expect } from "vitest";

import {
  parseTraceBundle,
  TraceBundleSchema,
  SCHEMA_VERSION,
  type TraceBundle,
} from "../src/index.js";

const sample: TraceBundle = {
  schemaVersion: SCHEMA_VERSION,
  session: {
    schemaVersion: SCHEMA_VERSION,
    sessionId: "sess_1",
    agent: { id: "agent_1", name: "support-bot", version: "3" },
    source: { framework: "pipecat", sdkVersion: "0.1.0" },
    providers: { transport: "twilio", stt: "deepgram", llm: "openai", tts: "cartesia" },
    startedAt: "2026-07-09T15:00:00.000Z",
    endedAt: "2026-07-09T15:00:12.000Z",
    durationMs: 12000,
    status: "completed",
    consent: { recording: true, piiRedacted: false },
    metadata: {},
  },
  turns: [
    {
      turnId: "turn_1",
      sessionId: "sess_1",
      index: 0,
      user: { transcript: "book a table", startMs: 900, endMs: 2400 },
      agent: { transcript: "sure, for how many?", startMs: 2760, endMs: 3980 },
      status: "completed",
      interruption: null,
      error: null,
      audio: { inputRef: "audio_in_0", outputRef: "audio_out_0" },
    },
  ],
  spans: [
    {
      spanId: "span_llm_1",
      turnId: "turn_1",
      type: "llm",
      name: "openai:gpt-realtime",
      provider: "openai",
      startMs: 2400,
      endMs: 2760,
      status: "ok",
      attributes: { firstTokenMs: 212 },
      error: null,
    },
  ],
  events: [],
  audio: [],
};

describe("trace bundle schema v0", () => {
  it("accepts a minimal valid bundle", () => {
    const parsed = TraceBundleSchema.parse(sample);
    expect(parsed.session.sessionId).toBe("sess_1");
    expect(parsed.turns).toHaveLength(1);
  });

  it("applies defaults for optional collections", () => {
    const parsed = TraceBundleSchema.parse({
      schemaVersion: SCHEMA_VERSION,
      session: sample.session,
    });
    expect(parsed.turns).toEqual([]);
    expect(parsed.spans).toEqual([]);
  });

  it("rejects an unknown schema_version via the safe parser", () => {
    const result = parseTraceBundle({ ...sample, schemaVersion: "9.9" });
    expect(result.ok).toBe(false);
  });

  it("keeps spans as raw timings the SDK sends (start <= end)", () => {
    const parsed = TraceBundleSchema.parse(sample);
    const span = parsed.spans[0];
    expect(span).toBeDefined();
    expect(span!.startMs).toBeLessThanOrEqual(span!.endMs);
  });
});
