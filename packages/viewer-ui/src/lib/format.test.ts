import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatMeasurement,
  formatMs,
  formatRelativeTime,
} from "./format";

describe("formatMs", () => {
  it("rounds and suffixes ms", () => {
    expect(formatMs(240.4)).toBe("240ms");
  });
  it("renders an em-dash for null/undefined", () => {
    expect(formatMs(null)).toBe("—");
    expect(formatMs(undefined)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("uses ms under a second and s above", () => {
    expect(formatDuration(720)).toBe("720ms");
    expect(formatDuration(7210)).toBe("7.2s");
  });

  it("renders unavailable duration as an em-dash", () => {
    expect(formatDuration(null)).toBe("—");
  });
});

describe("formatMeasurement", () => {
  it("renders duration units as compact time", () => {
    expect(formatMeasurement(240.4, "ms")).toBe("240ms");
    expect(formatMeasurement(1.5, "s")).toBe("1.5s");
  });

  it("renders booleans as yes/no", () => {
    expect(formatMeasurement(true, "1")).toBe("yes");
    expect(formatMeasurement(false, "1")).toBe("no");
  });

  it("keeps a dimensionless ratio (unit '1') as a bare number", () => {
    expect(formatMeasurement(0.25, "1")).toBe("0.25");
  });

  it("pairs a real unit with its value instead of mislabelling it as ms", () => {
    expect(formatMeasurement(-21.4, "dbfs")).toBe("-21.4 dbfs");
    expect(formatMeasurement(5, "count")).toBe("5 count");
    // OpenTelemetry annotation braces are stripped for display.
    expect(formatMeasurement(42, "{character}")).toBe("42 character");
  });

  it("renders an em-dash for a null/undefined value", () => {
    expect(formatMeasurement(null, "dbfs")).toBe("—");
    expect(formatMeasurement(undefined, "ms")).toBe("—");
  });
});

describe("formatRelativeTime", () => {
  const now = 1_753_000_000_000; // ms
  const nano = (secondsAgo: number) =>
    String(BigInt(now / 1000 - secondsAgo) * 1_000_000_000n);

  it("handles nanosecond precision without overflow", () => {
    expect(formatRelativeTime(nano(5), now)).toBe("5s ago");
    expect(formatRelativeTime(nano(120), now)).toBe("2m ago");
    expect(formatRelativeTime(nano(7200), now)).toBe("2h ago");
    expect(formatRelativeTime(nano(172_800), now)).toBe("2d ago");
  });
});
