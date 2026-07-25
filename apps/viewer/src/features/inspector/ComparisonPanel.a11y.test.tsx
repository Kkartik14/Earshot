import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ComparisonResult } from "./comparison";
import { ComparisonPanel } from "./ComparisonPanel";

// The panel's hooks are mocked so each state — a resolved diff, an incomparable
// pair, and every baseline/stale-analysis refusal — is driven deterministically
// without a live backend. This mirrors App.test.tsx's hook-mocking approach.
const state = vi.hoisted(() => ({
  incidents: null as unknown,
  comparison: null as unknown,
}));

vi.mock("../../api/hooks", () => ({
  useIncidents: () => state.incidents,
  useComparison: () => state.comparison,
}));

const incidentsReady = {
  data: {
    items: [
      {
        bundle_id: "kg-1",
        session_id: "good-call",
        framework: "pipecat",
        finality: "final",
      },
      {
        bundle_id: "fix",
        session_id: "this-call",
        framework: "custom",
        finality: "final",
      },
    ],
  },
  isPending: false,
  isError: false,
};

const baseResult: ComparisonResult = {
  bundle_id: "fix",
  known_good_bundle_id: "kg-1",
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

function openAndPick() {
  fireEvent.click(
    screen.getByRole("button", { name: /compare against a known-good session/i }),
  );
  fireEvent.change(screen.getByLabelText("Known-good session"), {
    target: { value: "kg-1" },
  });
}

beforeEach(() => {
  state.incidents = incidentsReady;
  state.comparison = { data: undefined, isPending: false, isError: false };
});

describe("ComparisonPanel", () => {
  it("exposes a keyboard-reachable, accessibly-named baseline picker", () => {
    render(<ComparisonPanel bundleId="fix" />);
    const toggle = screen.getByRole("button", {
      name: /compare against a known-good session/i,
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    toggle.focus();
    expect(toggle).toHaveFocus();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const select = screen.getByLabelText("Known-good session");
    expect(select).toBeInTheDocument();
    // The incident never offers itself as its own baseline.
    expect(
      within(select as HTMLSelectElement).queryByText(/this-call/),
    ).not.toBeInTheDocument();
    expect(
      within(select as HTMLSelectElement).getByText(/good-call/),
    ).toBeInTheDocument();
  });

  it("renders an incomparable metric pair as incomparable, with its reason", () => {
    state.comparison = {
      data: {
        ...baseResult,
        turn_metric_availability_changes: [
          {
            turn_id: "turn-2",
            metric: "first_token_latency",
            known_good_availability: "available",
            incident_availability: "not_observed",
            comparable: false,
          },
        ],
      },
      isPending: false,
      isError: false,
    };
    render(<ComparisonPanel bundleId="fix" />);
    openAndPick();

    const region = within(screen.getByRole("region", { name: /availability changes/i }));
    expect(region.getByText("incomparable")).toBeInTheDocument();
    expect(
      region.getByText(/not the same quantity, so no delta is computed/i),
    ).toBeInTheDocument();
  });

  it("renders a real latency delta with its direction and unit", () => {
    state.comparison = {
      data: {
        ...baseResult,
        turn_metric_deltas: [
          {
            turn_id: "turn-1",
            metric: "response_latency",
            unit: "ms",
            known_good_value: 100,
            incident_value: 250,
            delta: 150,
          },
        ],
      },
      isPending: false,
      isError: false,
    };
    render(<ComparisonPanel bundleId="fix" />);
    openAndPick();
    expect(screen.getByText("+150ms")).toBeInTheDocument();
  });

  it("states an empty diff as 'nothing changed', never as a blank panel", () => {
    state.comparison = { data: baseResult, isPending: false, isError: false };
    render(<ComparisonPanel bundleId="fix" />);
    openAndPick();
    expect(
      screen.getByText(/nothing changed relative to this baseline/i),
    ).toBeInTheDocument();
  });

  it("renders a missing baseline as an explicit, coded state", () => {
    state.comparison = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(404, "EARSHOT_KNOWN_GOOD_NOT_FOUND"),
    };
    render(<ComparisonPanel bundleId="fix" />);
    openAndPick();
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/known-good incident not found/i);
    expect(status).toHaveTextContent(/EARSHOT_KNOWN_GOOD_NOT_FOUND/);
  });

  it("renders a stale analysis as a clean message, never a blank", () => {
    state.comparison = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(409, "EARSHOT_ANALYSIS_BINDING_MISMATCH"),
    };
    render(<ComparisonPanel bundleId="fix" />);
    openAndPick();
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/stale analysis/i);
    expect(status).toHaveTextContent(/EARSHOT_ANALYSIS_BINDING_MISMATCH/);
  });
});
