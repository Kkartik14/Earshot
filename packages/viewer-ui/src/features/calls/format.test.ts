import { describe, expect, it } from "vitest";
import { costReadout, formatClock, formatSeconds, formatUsd, gapReadout } from "./format";
import type { AudibleGap } from "./api";

const gap = (overrides: Partial<AudibleGap>): AudibleGap => ({
  status: "measured",
  value_ms: null,
  uncertainty_ms: null,
  bound_ms: null,
  user_end_ms: null,
  agent_start_ms: null,
  reason: null,
  basis: null,
  ...overrides,
});

describe("formatUsd (nano-USD)", () => {
  it.each([
    [3_672_825, "$0.00367"],
    [4_169_037, "$0.00417"],
    [1_000_000, "$0.001"],
    [37_125, "$0.0000371"],
    [1, "$0.000000001"],
    [0, "$0"],
    [12_345_000_000, "$12.35"],
    [1_000_000_000, "$1.00"],
  ])("%d → %s", (nano, text) => {
    expect(formatUsd(nano)).toBe(text);
  });
});

describe("costReadout", () => {
  it("shows a complete cost as its amount", () => {
    expect(
      costReadout({
        lower_bound_nano_usd: 1_000_000,
        estimated_nano_usd: null,
        unknown_items: 0,
        complete: true,
      }),
    ).toEqual({ amount: "$0.001", estimate: null });
  });

  it("marks an incomplete cost as a lower bound and keeps the estimate apart", () => {
    expect(
      costReadout({
        lower_bound_nano_usd: 3_672_825,
        estimated_nano_usd: 4_169_037,
        unknown_items: 2,
        complete: false,
      }),
    ).toEqual({ amount: "≥ $0.00367 · 2 unknown", estimate: "+ ~$0.00417 (estimated)" });
  });

  it("never renders a call with no cost item as $0", () => {
    const readout = costReadout({
      lower_bound_nano_usd: null,
      estimated_nano_usd: null,
      unknown_items: 0,
      complete: false,
    });
    expect(readout).toEqual({ amount: "—", estimate: null });
  });

  it("keeps a known zero lower bound distinct from unknown items", () => {
    expect(
      costReadout({
        lower_bound_nano_usd: 0,
        estimated_nano_usd: null,
        unknown_items: 1,
        complete: false,
      }).amount,
    ).toBe("≥ $0 · 1 unknown");
  });
});

describe("gapReadout", () => {
  it("shows a measured gap with its uncertainty", () => {
    expect(gapReadout(gap({ value_ms: 1510, uncertainty_ms: 30 }))).toEqual({
      value: "1.51 s ± 0.03",
      reason: null,
    });
  });

  it("rounds the uncertainty to 10 ms, a half up, never below what was measured", () => {
    // Pipecat turn 2's measured ± is 75 ms. 0.075 has no exact binary
    // form and is stored just below it, so a plain toFixed(2) would print 0.07.
    expect(gapReadout(gap({ value_ms: 1510, uncertainty_ms: 75 })).value).toBe(
      "1.51 s ± 0.08",
    );
    expect(gapReadout(gap({ value_ms: 1490, uncertainty_ms: 48 })).value).toBe(
      "1.49 s ± 0.05",
    );
  });

  it("names a talk-over instead of a number", () => {
    expect(gapReadout(gap({ status: "talk_over" })).value).toBe("talk-over");
  });

  it("shows an upper bound for a recorder timeline discontinuity, with the reason", () => {
    const readout = gapReadout(
      gap({
        status: "not_measurable",
        bound_ms: 3080,
        uncertainty_ms: 20,
        reason: "recorder_timeline_discontinuity",
      }),
    );
    expect(readout.value).toBe("≤ 3.08 s");
    expect(readout.reason).toMatch(/timeline/i);
  });

  it("shows a dash, never a zero, when there is no bound", () => {
    const readout = gapReadout(
      gap({ status: "not_measurable", reason: "no_agent_audio" }),
    );
    expect(readout.value).toBe("—");
    expect(readout.reason).toMatch(/agent/i);
  });

  it("explains when speech-edge uncertainty exceeds the 100 ms measurement limit", () => {
    const readout = gapReadout(
      gap({
        status: "not_measurable",
        uncertainty_ms: 118,
        reason: "boundary_uncertainty_too_high",
      }),
    );
    expect(readout.value).toBe("—");
    expect(readout.reason).toMatch(/uncertain/i);
  });
});

describe("time formats", () => {
  it("formats latencies in seconds and unknowns as a dash", () => {
    expect(formatSeconds(1800)).toBe("1.80 s");
    expect(formatSeconds(null)).toBe("—");
  });

  it("formats call-axis times as a clock, including before the recording", () => {
    expect(formatClock(1310)).toBe("0:01.3");
    expect(formatClock(75_960)).toBe("1:15.9");
    expect(formatClock(-200)).toBe("-0:00.2");
    expect(formatClock(3_723_400)).toBe("1:02:03.4");
  });
});
