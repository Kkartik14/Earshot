import { describe, expect, it } from "vitest";
import { budgetFor, rankByP95, summarizeGroups, type MetricGroup } from "./fleet";

const group = (over: Partial<MetricGroup>): MetricGroup => ({
  group: "g",
  availability: "available",
  basis: "provider_stage_direct",
  confidence: "measured",
  limitation: null,
  turn_count: 10,
  available_count: 10,
  average_ms: 200,
  minimum_ms: 100,
  maximum_ms: 400,
  p50_ms: 180,
  p95_ms: 300,
  ...over,
});

describe("summarizeGroups", () => {
  it("sums turns and reports coverage across groups", () => {
    const summary = summarizeGroups([
      group({ turn_count: 8, available_count: 8 }),
      group({ turn_count: 2, available_count: 1 }),
    ]);
    expect(summary.turns).toBe(10);
    expect(summary.measured).toBe(9);
    expect(summary.coveragePct).toBe(90);
  });

  it("reports the worst p95 and best p50, never an averaged percentile", () => {
    const summary = summarizeGroups([
      group({ p50_ms: 180, p95_ms: 300 }),
      group({ p50_ms: 120, p95_ms: 720 }),
    ]);
    expect(summary.worstP95).toBe(720);
    expect(summary.bestP50).toBe(120);
  });

  it("ignores groups with no measured percentile", () => {
    const summary = summarizeGroups([
      group({ p50_ms: null, p95_ms: null, available_count: 0 }),
      group({ p50_ms: 200, p95_ms: 400 }),
    ]);
    expect(summary.worstP95).toBe(400);
    expect(summary.bestP50).toBe(200);
  });

  it("handles an empty fleet without dividing by zero", () => {
    const summary = summarizeGroups([]);
    expect(summary.turns).toBe(0);
    expect(summary.coveragePct).toBeNull();
    expect(summary.worstP95).toBeNull();
  });
});

describe("rankByP95", () => {
  it("orders slowest first and sinks unmeasured groups", () => {
    const ranked = rankByP95([
      group({ group: "a", p95_ms: 300 }),
      group({ group: "b", p95_ms: null }),
      group({ group: "c", p95_ms: 720 }),
    ]);
    expect(ranked.map((g) => g.group)).toEqual(["c", "a", "b"]);
  });
});

describe("budgetFor", () => {
  it("knows the first-token budget and returns null otherwise", () => {
    expect(budgetFor("first_token_ms")).toBe(500);
    expect(budgetFor("turn_duration_ms")).toBeNull();
  });
});
