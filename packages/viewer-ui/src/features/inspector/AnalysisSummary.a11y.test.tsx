import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnalysisSummaryPanel, type AnalysisSummaryView } from "./AnalysisSummary";

const view: AnalysisSummaryView = {
  analyzerName: "earshot.deterministic",
  analyzerVersion: "1.0.4",
  inputDigest: "c81631093d9cb7f256e117e2738848fc7d8acea62742d99e91d8a5dd527c1cb6",
  generatedAtUnixNano: "1700000000000000000",
  diagnosisCount: 2,
  turnCount: 5,
};

describe("AnalysisSummaryPanel", () => {
  it("surfaces the analyzer identity and the evidence digest it is bound to", () => {
    render(<AnalysisSummaryPanel status="ready" reason={null} view={view} />);
    const panel = within(screen.getByRole("region", { name: "Analysis" }));
    expect(panel.getByText(/earshot\.deterministic · 1\.0\.4/)).toBeInTheDocument();
    expect(panel.getByText(/c81631093d9c/)).toBeInTheDocument();
    expect(panel.getByText("diagnoses raised").parentElement).toHaveTextContent("2");
  });

  it("renders an unprojected analysis as unknown, never as zero turns", () => {
    render(
      <AnalysisSummaryPanel
        status="ready"
        reason={null}
        view={{ ...view, turnCount: null }}
      />,
    );
    const row = screen.getByText("turns analyzed").parentElement;
    expect(row).toHaveTextContent(/not projected/i);
    expect(row).not.toHaveTextContent(/\b0\b/);
  });

  it("states an unavailable analysis explicitly with its reason", () => {
    render(
      <AnalysisSummaryPanel
        status="unavailable"
        reason="EARSHOT_ANALYSIS_NOT_AVAILABLE"
        view={null}
      />,
    );
    const status = within(screen.getByRole("region", { name: "Analysis" })).getByRole(
      "status",
    );
    expect(status).toHaveTextContent(/no derived analysis/i);
    expect(status).toHaveTextContent(/EARSHOT_ANALYSIS_NOT_AVAILABLE/);
    expect(status).toHaveTextContent(/unavailable, not empty/i);
  });
});
