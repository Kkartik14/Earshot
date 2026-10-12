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

// Explanation fixture assembled from the captured incident and analysis.
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

// Stored analysis response with its evidence binding.
const analysisResponse = {
  bundle_id: "fixture-bundle",
  analyzer_version: "1.0.4",
  input_digest: "c81631093d9cb7f256e117e2738848fc7d8acea62742d99e91d8a5dd527c1cb6",
  generated_at_unix_nano: "1784631258902097000",
  analysis: analysisFixture,
};

// Contradiction response citing an operation in this session.
const contradictionReport = {
  bundle_id: "fixture-bundle",
  analyzer_version: "fixture",
  input_digest: "a".repeat(64),
  contradictions: [
    {
      kind: "render_claim_conflict",
      summary: "render_observed_while_coverage_not_observed",
      evidence_ids: ["operation-llm-0-5"],
      boundary: "render",
      turn_id: "turn-0",
      subject: "turn-0",
    },
  ],
};

// Not-observed response covering a gap, metric limitation, and uncounted omission.
const notObservedReport = {
  coverage_gaps: [
    { signal: "client.render", availability: "not_observed", reason: "collector_absent" },
  ],
  limitations: [
    {
      scope: "turn",
      turn_id: "turn-0",
      metric: "render_start_response_latency",
      availability: "not_observed",
      limitation: "render_not_observed",
      evidence_ids: ["operation-llm-0-5"],
    },
  ],
  omissions: [
    {
      omission_id: "om-1",
      capture_class: "transcript_text",
      reason: "policy_redacted",
      count: null,
      source_refs: [],
    },
  ],
};

// Summary endpoint response for this session.
const evidenceSummaryReport = {
  session_id: "fixture-session",
  counts: {
    turn_count: 1,
    operation_count: 3,
    event_count: 2,
    quality_sample_count: 1,
    failed_operation_count: 1,
    diagnosis_count: 1,
    boundary_diagnosis_count: 0,
    coverage_gap_count: 1,
    contradiction_count: 0,
  },
  diagnoses: [],
  first_abnormal_boundary: { found: false, reason: "no_boundary_diagnosis" },
};

/** Checkpoint-recovered form of the fixture incident. */
const recoveredFixture = {
  ...incidentFixture,
  profile: {
    ...incidentFixture.profile,
    manifest: {
      ...incidentFixture.profile.manifest,
      finality: "provisional",
      completeness: "incomplete",
      recovery: {
        method: "checkpoint_journal",
        reason: "process_terminated_before_close",
        close_observed: false,
        journal_id: "6c64ca59b0544136bc4371db66600b11",
        last_sequence: 41,
        torn_tail_bytes: 37,
        discarded_records: 0,
        journal_complete: true,
        recoverer: { name: "earshot", version: "0.1.0", sdk_version: "0.1.0" },
        attributes: {},
      },
    },
  },
};

/** Partial browser capture form of the fixture, with no checkpoint journal. */
const captureFixture = {
  ...incidentFixture,
  profile: {
    ...incidentFixture.profile,
    manifest: {
      ...incidentFixture.profile.manifest,
      finality: "provisional",
      completeness: "incomplete",
      recovery: {
        method: "browser_capture_batch",
        reason: "capture_batch_flushed_before_close",
        close_observed: false,
        journal_id: null,
        last_sequence: null,
        torn_tail_bytes: 0,
        discarded_records: 0,
        journal_complete: true,
        recoverer: {
          name: "earshot.capture_api",
          version: "0.1.0",
          sdk_version: "0.1.0",
        },
        attributes: {},
      },
    },
  },
};

/** Fixture with a reference to media held by an external custodian. */
const custodyFixture = {
  ...incidentFixture,
  profile: {
    ...incidentFixture.profile,
    media_refs: [
      {
        media_id: "media-1",
        session_id: incidentFixture.profile.session.session_id,
        stream_id: "stream-out",
        media_kind: "audio",
        content_type: "audio/wav",
        integrity: "opaque_handle",
        custodian: "provider.vapi",
        clock_domain_id: "media-1",
        capture_class: "audio",
        locator: { uri: "https://media.example.com/1.wav", access: "governed" },
        attributes: {},
      },
    ],
  },
};

function renderInspector({
  contradictions,
  incident: incidentOverride,
}: { contradictions?: unknown; incident?: unknown } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  });
  client.setQueryData(["incident", "fix"], incidentOverride ?? incidentFixture);
  client.setQueryData(["explanation", "fix"], explanation);
  client.setQueryData(["analysis", "fix"], analysisResponse);
  client.setQueryData(["not-observed", "fix"], notObservedReport);
  client.setQueryData(["evidence-summary", "fix"], evidenceSummaryReport);
  if (contradictions !== undefined) {
    client.setQueryData(["contradictions", "fix"], contradictions);
  }
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
    expect(screen.getByRole("heading", { name: /diagnoses/i })).toBeInTheDocument();
    expect(screen.getByText("operation.failed")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "operation-llm-0-5" }));
    expect(screen.getByRole("dialog", { name: /llm detail/i })).toBeInTheDocument();
  });

  it("surfaces backend contradictions and selects the conflicting operation", () => {
    renderInspector({ contradictions: contradictionReport });

    const panel = within(screen.getByRole("region", { name: /contradictions/i }));
    expect(panel.getByText("render claim conflict")).toBeInTheDocument();
    expect(
      panel.getByText("render observed while coverage not observed"),
    ).toBeInTheDocument();

    fireEvent.click(panel.getByRole("button", { name: "operation-llm-0-5" }));
    expect(screen.getByRole("dialog", { name: /llm detail/i })).toBeInTheDocument();
  });

  it("surfaces what the evidence does not observe as explicit unknowns", () => {
    renderInspector();
    const region = within(
      screen.getByRole("region", { name: /what the evidence does not tell us/i }),
    );
    expect(region.getByText("client.render")).toBeInTheDocument();
    expect(region.getByText(/collector absent/i)).toBeInTheDocument();
    expect(region.getByText(/render not observed/i)).toBeInTheDocument();
    expect(region.getByText("count not recorded")).toBeInTheDocument();
    expect(region.queryByText(/0 omitted/)).toBeNull();
  });

  it("surfaces the analyzer's own output binding for the incident", () => {
    renderInspector();
    const panel = within(screen.getByRole("region", { name: "Analysis" }));
    expect(panel.getByText(/earshot\.deterministic · 1\.0\.4/)).toBeInTheDocument();
    expect(panel.getByText(/c81631093d9c/)).toBeInTheDocument();
    expect(panel.getByText("turns analyzed").parentElement).toHaveTextContent("5");
  });

  it("renders unassigned session-level measurements with their units", () => {
    renderInspector();
    expect(
      screen.getByRole("region", { name: /session-level facts/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("round_trip_time")).toBeInTheDocument();
    expect(screen.getByText("180ms")).toBeInTheDocument();
  });

  it("does not claim recovery for an artifact its producer cleanly closed", () => {
    renderInspector();
    expect(screen.queryByRole("region", { name: /recovered artifact/i })).toBeNull();
  });

  it("renders a persistent recovered strip carrying the reason and the loss", () => {
    renderInspector({ incident: recoveredFixture });

    const strip = screen.getByRole("region", { name: /recovered artifact/i });
    expect(within(strip).getByRole("status")).toHaveTextContent(
      /process terminated before close/i,
    );
    expect(within(strip).getByText(/RECOVERED — NOT A CLEAN CLOSE/)).toBeInTheDocument();
    expect(within(strip).getByText(/close observed:/i)).toHaveTextContent("no");
    expect(
      within(strip).getByText(
        /evidence was lost at the end of the journal \(37 bytes\)/i,
      ),
    ).toBeInTheDocument();
    expect(strip.compareDocumentPosition(screen.getByRole("heading", { level: 1 }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("marks a browser capture batch as partial without inventing a journal", () => {
    renderInspector({ incident: captureFixture });

    const strip = screen.getByRole("region", { name: /recovered artifact/i });
    expect(
      within(strip).getByText(/PARTIAL CAPTURE — NOT A CLEAN CLOSE/),
    ).toBeInTheDocument();
    expect(within(strip).getByRole("status")).toHaveTextContent(
      /session still in progress; the browser drained telemetry mid-call/i,
    );
    expect(within(strip).getByRole("status")).not.toHaveTextContent(
      /checkpoint journal/i,
    );
    expect(within(strip).getByRole("status")).not.toHaveTextContent(/the process ended/i);
    expect(within(strip).getByText(/close observed:/i)).toHaveTextContent("no");
    expect(within(strip).queryByText(/^journal /i)).toBeNull();
  });

  it("shows no custody panel for a session that references no media", () => {
    renderInspector();
    expect(screen.queryByRole("region", { name: /media custody/i })).toBeNull();
  });

  it("renders media custody without loading a single byte of the media", () => {
    const { container } = renderInspector({ incident: custodyFixture });

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText("provider.vapi")).toBeInTheDocument();
    expect(panel.getByText("cannot align")).toBeInTheDocument();
    expect(container.querySelectorAll("audio, video, source, [src]")).toHaveLength(0);
    expect(screen.getByRole("link", { name: /open at the custodian/i })).toHaveAttribute(
      "href",
      "https://media.example.com/1.wav",
    );
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
