import { readFileSync } from "node:fs";

import { describe, it, expect } from "vitest";

import { analyzeUnknown } from "../src/index.js";

const raw = readFileSync(
  new URL("./fixtures/livekit-call.json", import.meta.url),
  "utf8",
);
const bundle: unknown = JSON.parse(raw);

describe("analyzeCall on a LiveKit-shaped call", () => {
  const call = analyzeUnknown(bundle);

  it("finds both turns in index order", () => {
    expect(call.turns).toHaveLength(2);
    expect(call.turns.map((t) => t.index)).toEqual([0, 1]);
  });

  it("derives TTFT as EOU -> first LLM token", () => {
    // turn 0: llm start 3300 + firstTokenMs 280 - EOU 3200 = 380
    expect(call.turns[0]!.metrics.ttftMs).toBe(380);
    // turn 1: 7700 + 240 - 7600 = 340
    expect(call.turns[1]!.metrics.ttftMs).toBe(340);
  });

  it("derives response latency as EOU -> first audible output", () => {
    expect(call.turns[0]!.metrics.responseMs).toBe(500); // 3700 - 3200
    expect(call.turns[1]!.metrics.responseMs).toBe(1200); // 8800 - 7600 (tool adds cost)
  });

  it("reports provider TTS TTFB and endpoint delay", () => {
    expect(call.turns[0]!.metrics.ttfbMs).toBe(150);
    expect(call.turns[0]!.metrics.endpointMs).toBe(120);
  });

  it("sums tool time per turn", () => {
    expect(call.turns[0]!.metrics.toolMs).toBe(0);
    expect(call.turns[1]!.metrics.toolMs).toBe(430); // 8480 - 8050
  });

  it("builds a waterfall with turn-relative offsets", () => {
    const t0 = call.turns[0]!;
    expect(t0.spans).toHaveLength(4);
    expect(t0.spans[0]!.offsetMs).toBe(0); // first span sits at the turn start
    expect(t0.spans[0]!.type).toBe("stt");
    const t1 = call.turns[1]!;
    expect(t1.spans).toHaveLength(6);
  });

  it("summarizes the call", () => {
    expect(call.summary.turnCount).toBe(2);
    expect(call.summary.completedTurns).toBe(1);
    expect(call.summary.interruptedTurns).toBe(1);
    expect(call.summary.interruptionRate).toBe(0.5);
    expect(call.summary.p50ResponseMs).toBe(500);
    expect(call.summary.p95ResponseMs).toBe(1200);
    expect(call.summary.avgTtftMs).toBe(360); // (380 + 340) / 2
  });

  it("rejects an invalid bundle", () => {
    expect(() => analyzeUnknown({ schemaVersion: "0.1" })).toThrow(
      /invalid trace bundle/,
    );
  });
});
