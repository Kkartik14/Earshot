import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import {
  availabilityChangeReason,
  comparisonIsEmpty,
  comparisonUnavailable,
  type AvailabilityChange,
  type ComparisonResult,
} from "./comparison";

const emptyResult: ComparisonResult = {
  bundle_id: "incident",
  known_good_bundle_id: "baseline",
  analyzer_version: "1.0.0",
  input_digest: "a".repeat(64),
  known_good_input_digest: "b".repeat(64),
  diagnoses_added: [],
  diagnoses_removed: [],
  turn_metric_deltas: [],
  turn_metric_availability_changes: [],
  unmatched_turns: { only_in_incident: [], only_in_known_good: [] },
  coverage_gaps_new: [],
  coverage_gaps_removed: [],
  contradictions_new: [],
};

const change = (over: Partial<AvailabilityChange>): AvailabilityChange => ({
  turn_id: "turn-0",
  metric: "response_latency",
  known_good_availability: "available",
  incident_availability: "not_observed",
  comparable: false,
  ...over,
});

describe("comparisonUnavailable", () => {
  it("names which baseline side is missing, purged, or unanalysed", () => {
    expect(
      comparisonUnavailable(new ApiError(404, "EARSHOT_KNOWN_GOOD_NOT_FOUND")).title,
    ).toMatch(/not found/i);
    expect(
      comparisonUnavailable(new ApiError(410, "EARSHOT_KNOWN_GOOD_PURGED")).title,
    ).toMatch(/purged/i);
    expect(
      comparisonUnavailable(
        new ApiError(404, "EARSHOT_KNOWN_GOOD_ANALYSIS_NOT_AVAILABLE"),
      ).title,
    ).toMatch(/no analysis/i);
  });

  it("reads a stale-analysis conflict as exactly that, carrying its code", () => {
    const state = comparisonUnavailable(
      new ApiError(409, "EARSHOT_ANALYSIS_BINDING_MISMATCH"),
    );
    expect(state.code).toBe("EARSHOT_ANALYSIS_BINDING_MISMATCH");
    expect(state.title).toMatch(/stale/i);
    expect(state.detail).toMatch(/withheld|regenerated/i);
  });

  it("reports a non-API failure as an unanswered backend, never as identical", () => {
    const state = comparisonUnavailable(new Error("network down"));
    expect(state.code).toBe("unavailable");
    expect(state.detail).toMatch(/did not answer/i);
  });
});

describe("comparisonIsEmpty", () => {
  it("is true only when every diff dimension is empty", () => {
    expect(comparisonIsEmpty(emptyResult)).toBe(true);
  });

  it("is false when any dimension carries a change", () => {
    expect(
      comparisonIsEmpty({
        ...emptyResult,
        unmatched_turns: { only_in_incident: ["turn-9"], only_in_known_good: [] },
      }),
    ).toBe(false);
  });
});

describe("availabilityChangeReason", () => {
  it("states an incomparable pair as incomparable, with both availabilities", () => {
    const reason = availabilityChangeReason(change({ comparable: false }));
    expect(reason).toMatch(/incomparable/i);
    expect(reason).toMatch(/available/);
    expect(reason).toMatch(/not observed/);
    expect(reason).not.toMatch(/^0/);
  });

  it("explains an incomparable pair whose two sides share an availability", () => {
    const reason = availabilityChangeReason(
      change({ comparable: false, incident_availability: "available" }),
    );
    expect(reason).toMatch(/incomparable/i);
    expect(reason).toMatch(/different reference points/i);
  });

  it("names a genuine availability change when the pair is comparable", () => {
    const reason = availabilityChangeReason(change({ comparable: true }));
    expect(reason).toMatch(/availability changed/i);
    expect(reason).not.toMatch(/incomparable/i);
  });
});
