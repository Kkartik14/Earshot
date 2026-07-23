import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import { SessionInspector } from "./SessionInspector";
import type { AnalysisLike, IncidentLike } from "./timeline";

const incident = incidentFixture as unknown as IncidentLike;
const analysis = analysisFixture as unknown as AnalysisLike;

// The backend-authored explanation shape the viewer consumes, assembled from the
// captured fixtures exactly as the transform tests do.
const explanation = {
  bundle_id: "fixture-bundle",
  session_id: "fixture-session",
  session_status: incident.profile.session?.status ?? "unknown",
  finality: "final",
  completeness: "complete",
  analyzer_version: "fixture",
  limitations: [],
  coverage: incident.profile.coverage ?? [],
  omissions: [],
  diagnoses: [
    {
      diagnosis_id: "operation_failed.1",
      code: "operation.failed",
      summary: "the llm operation failed",
      confidence: "measured",
      evidence_ids: ["operation-llm-0-5"],
      limitations: [],
    },
  ],
  unassigned_operations: [],
  unassigned_measurements: [
    {
      name: "round_trip_time",
      value: 180,
      unit: "ms",
      aggregation: "instant",
      basis: "provider_measurement",
      confidence: "measured",
      evidence_ids: ["quality-webrtc"],
    },
  ],
  turns: analysis.projections!.turns.map((turn) => ({
    turn_id: turn.turn_id,
    metrics: turn.metrics,
    operations: incident.profile.operations
      .filter((operation) => operation.turn_id === turn.turn_id)
      .map((operation) => {
        const start = operation.started_at.monotonic_time_nano ?? "0";
        const end = operation.ended_at?.monotonic_time_nano;
        return {
          operation_id: operation.operation_id ?? undefined,
          operation_name: operation.operation_name,
          status: operation.status ?? "unknown",
          shape: end == null ? "point" : "interval",
          time_basis: "monotonic",
          clock_domain_id: operation.started_at.clock_domain_id,
          start_nano: start,
          duration_nano: end == null ? null : String(BigInt(end) - BigInt(start)),
          provider:
            typeof operation.attributes?.["gen_ai.provider.name"] === "string"
              ? operation.attributes["gen_ai.provider.name"]
              : null,
          model:
            typeof operation.attributes?.["gen_ai.request.model"] === "string"
              ? operation.attributes["gen_ai.request.model"]
              : null,
          evidence: operation.evidence,
          measurements: incident.profile.quality_samples
            .filter((sample) => sample.attributes?.["earshot.turn.id"] === turn.turn_id)
            .flatMap((sample) =>
              sample.measurements
                .filter((measurement) =>
                  measurement.name.startsWith(`earshot.${operation.operation_name}.`),
                )
                .map((measurement) => ({ ...measurement, evidence: sample.evidence })),
            ),
        };
      }),
    events: incident.profile.events
      .filter((event) => event.turn_id === turn.turn_id)
      .map((event) => ({
        event_name: event.event_name,
        time_basis: "monotonic",
        clock_domain_id: event.time?.clock_domain_id,
        at_nano: event.time?.monotonic_time_nano ?? "0",
        participant_id: event.participant_id,
        evidence: event.evidence,
      })),
  })),
};

function renderInspector() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  });
  client.setQueryData(["incident", "fix"], incidentFixture);
  client.setQueryData(["explanation", "fix"], explanation);
  const rendered = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/sessions/fix"]}>
        <Routes>
          <Route path="/sessions/:bundleId" element={<SessionInspector />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, client };
}

describe("SessionInspector focus management", () => {
  it("does not invent a latency budget from measured first-token values", () => {
    renderInspector();

    expect(screen.getByRole("button", { name: /^T03/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByText("p95 first-token").parentElement?.className).not.toMatch(
      /flagged/,
    );
  });

  it("opens a dialog focused on close and restores focus on Escape", () => {
    renderInspector();
    const turn = screen.getByRole("button", { name: /^T00/ });
    turn.focus();
    expect(turn).toHaveFocus();

    fireEvent.click(turn);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /close detail/i })).toHaveFocus();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(turn).toHaveFocus();
  });

  it("restores focus to the invoking turn when closed via the close button", () => {
    renderInspector();
    const turn = screen.getByRole("button", { name: /^T01/ });
    turn.focus();
    fireEvent.click(turn);

    const close = within(screen.getByRole("dialog")).getByRole("button", {
      name: /close detail/i,
    });
    fireEvent.click(close);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(turn).toHaveFocus();
  });

  it("surfaces backend diagnoses and selects the evidence operation on click", () => {
    renderInspector();
    // The session-level Diagnoses panel renders the analyzer's diagnosis.
    expect(screen.getByRole("heading", { name: /diagnoses/i })).toBeInTheDocument();
    expect(screen.getByText("operation.failed")).toBeInTheDocument();

    // Clicking the evidence chip opens the detail for that exact operation.
    fireEvent.click(screen.getByRole("button", { name: "operation-llm-0-5" }));
    expect(screen.getByRole("dialog", { name: /llm detail/i })).toBeInTheDocument();
  });

  it("renders unassigned session-level measurements with their units", () => {
    renderInspector();
    expect(
      screen.getByRole("region", { name: /session-level facts/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("round_trip_time")).toBeInTheDocument();
    expect(screen.getByText("180ms")).toBeInTheDocument();
  });

  it("preserves the open dialog and restore target across a data refresh", () => {
    const { client } = renderInspector();
    const turn = screen.getByRole("button", { name: /^T00/ });
    turn.focus();
    fireEvent.click(turn);

    act(() => {
      client.setQueryData(["explanation", "fix"], {
        ...explanation,
        analyzer_version: "refreshed",
      });
    });

    const dialog = screen.getByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: /close detail/i,
      }),
    );
    expect(turn).toHaveFocus();
  });
});
