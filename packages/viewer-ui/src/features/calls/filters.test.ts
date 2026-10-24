import { describe, expect, it } from "vitest";
import { EMPTY_FILTERS, filtersToQuery } from "./filters";

describe("filtersToQuery", () => {
  it("sends nothing for an empty form", () => {
    expect(filtersToQuery(EMPTY_FILTERS)).toEqual({ query: {}, errors: {} });
  });

  it("maps every control to its API parameter", () => {
    const { query, errors } = filtersToQuery({
      agent: " booking-agent ",
      outcome: "completed",
      tags: "vip, refund,,vip",
      minLatencyMs: "500",
      maxLatencyMs: "2000",
      minGapMs: "0",
      maxGapMs: "1500",
      minCostUsd: "0.001",
      maxCostUsd: "0.25",
      text: "window",
    });

    expect(errors).toEqual({});
    expect(query).toEqual({
      agent: "booking-agent",
      outcome: "completed",
      tag: ["vip", "refund"],
      min_reported_latency_ms: 500,
      max_reported_latency_ms: 2000,
      min_audible_gap_ms: 0,
      max_audible_gap_ms: 1500,
      min_cost_nano_usd: 1_000_000,
      max_cost_nano_usd: 250_000_000,
      q: "window",
    });
  });

  it("converts dollars to nano-USD exactly", () => {
    expect(filtersToQuery({ ...EMPTY_FILTERS, maxCostUsd: "0.000000001" }).query).toEqual(
      {
        max_cost_nano_usd: 1,
      },
    );
    expect(filtersToQuery({ ...EMPTY_FILTERS, minCostUsd: "1.1" }).query).toEqual({
      min_cost_nano_usd: 1_100_000_000,
    });
  });

  it("refuses a text search shorter than the server's 3 characters", () => {
    const { query, errors } = filtersToQuery({ ...EMPTY_FILTERS, text: "ab" });
    expect(errors.text).toMatch(/at least 3 characters/);
    expect(query).toEqual({});
  });

  it.each([
    ["minLatencyMs", "1.5"],
    ["maxGapMs", "-1"],
    ["maxGapMs", "ten"],
    ["minCostUsd", "1e3"],
    ["maxCostUsd", "0.0000000001"],
  ] as const)("refuses %s = %s", (field, value) => {
    const { errors } = filtersToQuery({ ...EMPTY_FILTERS, [field]: value });
    expect(errors[field]).toBeTruthy();
  });

  it("refuses more than 10 tags", () => {
    const tags = Array.from({ length: 11 }, (_, index) => `t${index}`).join(",");
    expect(filtersToQuery({ ...EMPTY_FILTERS, tags }).errors.tags).toMatch(/10/);
  });

  it("refuses a range whose minimum is above its maximum", () => {
    const { errors } = filtersToQuery({
      ...EMPTY_FILTERS,
      minLatencyMs: "900",
      maxLatencyMs: "100",
    });
    expect(errors.maxLatencyMs).toMatch(/minimum/i);
  });
});
