import { describe, expect, it } from "vitest";
import { formatDuration, formatMs, formatRelativeTime } from "./format";

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
