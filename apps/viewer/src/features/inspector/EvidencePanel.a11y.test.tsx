import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { NotObserved } from "./evidence";
import { EvidencePanel } from "./EvidencePanel";

// The panel's hooks are mocked so each state — a resolved projection, an empty
// examined result, and the missing/stale-analysis refusals — is driven
// deterministically without a live backend. Mirrors ComparisonPanel.a11y.test.
const state = vi.hoisted(() => ({
  notObserved: null as unknown,
  summary: null as unknown,
}));

vi.mock("../../api/hooks", () => ({
  useNotObserved: () => state.notObserved,
  useEvidenceSummary: () => state.summary,
}));

const emptyNotObserved: NotObserved = {
  coverage_gaps: [],
  limitations: [],
  omissions: [],
};

beforeEach(() => {
  state.notObserved = { data: undefined, isPending: false, isError: false };
  state.summary = { data: undefined, isPending: false, isError: false };
});

describe("EvidencePanel", () => {
  it("exposes a keyboard-reachable, accessibly-named region and disclosure", () => {
    state.notObserved = { data: emptyNotObserved, isPending: false, isError: false };
    render(<EvidencePanel bundleId="fix" />);

    expect(
      screen.getByRole("region", { name: /what the evidence does not tell us/i }),
    ).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /what was not observed/i });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    toggle.focus();
    expect(toggle).toHaveFocus();
    // Collapsing hides the detail; expanding brings it back — reachable by keyboard.
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/examined result/i)).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByText(/examined result/i)).toBeInTheDocument();
  });

  it("renders coverage gaps, limitations, and omissions as explicit unknowns with reasons", () => {
    state.notObserved = {
      data: {
        coverage_gaps: [
          {
            signal: "client.render",
            availability: "not_observed",
            reason: "collector_absent",
          },
        ],
        limitations: [
          { scope: "analysis", limitation: "cross_clock_latency_unavailable" },
          {
            scope: "turn",
            turn_id: "turn-2",
            metric: "first_token_latency",
            availability: "not_observed",
            limitation: "provider_stage_not_observed",
            evidence_ids: ["op-llm"],
          },
        ],
        omissions: [
          {
            omission_id: "om-1",
            capture_class: "transcript_text",
            reason: "policy_redacted",
            count: 3,
            source_refs: ["op-stt"],
          },
        ],
      },
      isPending: false,
      isError: false,
    };
    render(<EvidencePanel bundleId="fix" />);

    const gaps = within(screen.getByRole("region", { name: /coverage gaps/i }));
    expect(gaps.getByText("client.render")).toBeInTheDocument();
    expect(gaps.getByText(/collector absent/i)).toBeInTheDocument();

    const limits = within(screen.getByRole("region", { name: /^limitations$/i }));
    expect(limits.getByText(/cross clock latency unavailable/i)).toBeInTheDocument();
    expect(limits.getByText(/provider stage not observed/i)).toBeInTheDocument();
    expect(limits.getByText("op-llm")).toBeInTheDocument();

    const omissions = within(screen.getByRole("region", { name: /omissions/i }));
    expect(omissions.getByText(/policy redacted/i)).toBeInTheDocument();
    expect(omissions.getByText("3 omitted")).toBeInTheDocument();
  });

  it("states an uncountable omission as not recorded, never as zero", () => {
    state.notObserved = {
      data: {
        ...emptyNotObserved,
        omissions: [
          {
            omission_id: "om-2",
            capture_class: "audio_payload",
            reason: "not_retained",
            count: null,
            source_refs: [],
          },
        ],
      },
      isPending: false,
      isError: false,
    };
    render(<EvidencePanel bundleId="fix" />);

    expect(screen.getByText("count not recorded")).toBeInTheDocument();
    expect(screen.queryByText(/0 omitted/)).not.toBeInTheDocument();
  });

  it("states an examined empty projection as a finding, never as a blank", () => {
    state.notObserved = { data: emptyNotObserved, isPending: false, isError: false };
    render(<EvidencePanel bundleId="fix" />);
    expect(
      screen.getByText(
        /no coverage gaps, no analysis or turn limitations, and no policy omissions/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/examined result/i)).toBeInTheDocument();
  });

  it("renders a missing analysis as an explicit, coded state, not an empty panel", () => {
    state.notObserved = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(404, "EARSHOT_ANALYSIS_NOT_AVAILABLE"),
    };
    render(<EvidencePanel bundleId="fix" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/coverage unknown/i);
    expect(status).toHaveTextContent(/EARSHOT_ANALYSIS_NOT_AVAILABLE/);
    // "Unknown", never a reassuring "nothing is missing".
    expect(screen.queryByText(/examined result/i)).not.toBeInTheDocument();
  });

  it("renders a stale analysis as a clean coded message, never a blank", () => {
    state.notObserved = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(409, "EARSHOT_ANALYSIS_BINDING_MISMATCH"),
    };
    render(<EvidencePanel bundleId="fix" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/stale analysis/i);
    expect(status).toHaveTextContent(/EARSHOT_ANALYSIS_BINDING_MISMATCH/);
  });
});
