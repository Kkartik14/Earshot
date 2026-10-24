import { describe, expect, it } from "vitest";
import {
  bucketLabel,
  costPerMinute,
  coverage,
  perHundred,
  rangeQuery,
  share,
} from "./format";

describe("dashboard readouts", () => {
  it("names what a tile covers, and says so when it covers nothing", () => {
    expect(coverage(2, 3, "calls")).toBe("2 of 3 calls");
    expect(coverage(1, 1, "turns")).toBe("1 of 1 turn");
    expect(coverage(0, 0, "calls")).toBe("no calls");
  });

  it("never renders an unknown rate as zero", () => {
    expect(perHundred(2, 3)).toBe("66.7 per 100 turns");
    expect(perHundred(0, 4)).toBe("0 per 100 turns");
    expect(perHundred(null, 0)).toBe("—");
    expect(perHundred(0, 0)).toBe("—");
    expect(share(3_000, 180_000)).toBe("1.7%");
    expect(share(0, 60_000)).toBe("0%");
    expect(share(null, null)).toBe("—");
    expect(costPerMinute(60_000_000)).toBe("$0.06/min");
    expect(costPerMinute(0)).toBe("$0/min");
    expect(costPerMinute(null)).toBe("—");
  });

  it("labels buckets in UTC, as the server cuts them", () => {
    // 2027-01-15T08:00:00Z.
    expect(bucketLabel("1800000000000000000", "day")).toBe("Jan 15, 2027");
    expect(bucketLabel("1800000000000000000", "hour")).toBe("Jan 15, 08:00");
  });

  it("asks for the range that ends now, in exact Unix nanoseconds", () => {
    const now = Date.UTC(2026, 9, 20, 12, 34, 56, 789);
    expect(rangeQuery("24h", now)).toEqual({
      from: `${now - 86_400_000}000000`,
      to: `${now}000000`,
    });
    expect(rangeQuery("30d", now).from).toBe(`${now - 30 * 86_400_000}000000`);
  });
});
