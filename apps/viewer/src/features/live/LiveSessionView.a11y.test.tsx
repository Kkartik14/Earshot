import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { LiveSessionBody } from "./LiveSessionView";

const JOURNAL = "6c64ca59b0544136bc4371db66600b11";

/** A stand-in for the browser's EventSource that a test can drive by hand. */
class FakeEventSource {
  static readonly instances: FakeEventSource[] = [];
  readyState = 1;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListener): void {
    const set = this.listeners.get(name) ?? new Set<EventListener>();
    set.add(listener);
    this.listeners.set(name, set);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  emit(name: string, data: unknown, id?: string): void {
    act(() => {
      const message = new MessageEvent(name, {
        data: JSON.stringify(data),
        lastEventId: id ?? "",
      });
      for (const listener of this.listeners.get(name) ?? []) listener(message);
    });
  }

  fail(): void {
    act(() => {
      this.readyState = 2;
      this.onerror?.(new Event("error"));
    });
  }
}

const OPEN_PAYLOAD = {
  journal_id: JOURNAL,
  journal_format_version: 1,
  session_id: "s-live",
  bundle_id: "b-live",
  source: "journal",
  producer: { name: "earshot", version: "0.1.0" },
  clock_domain_id: "proc",
  started_at: { monotonic_time_nano: "1000", clock_domain_id: "proc" },
  capture_policy: {
    policy_id: "default",
    policy_version: "1",
    enabled_classes: ["metadata"],
  },
  recorder_limits: {
    max_records: 10000,
    max_capture_bytes: 1,
    max_raw_otlp_bytes: 1,
    max_value_bytes: 1,
  },
  in_progress: true,
  unknown_until_close: [
    "session_status",
    "session_ended_at",
    "session_duration",
    "turn_membership",
    "turn_metrics",
    "interruption_classification",
    "derived_analysis",
    "diagnoses",
  ],
};

function renderLive({ incidents }: { incidents?: unknown } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  });
  if (incidents !== undefined) {
    client.setQueryData(["incidents", { session_id: "s-live" }], incidents);
  }
  const rendered = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/live/s-live"]}>
        <Routes>
          <Route
            path="/live/:sessionId"
            element={
              <LiveSessionBody
                sessionId="s-live"
                tailOptions={{
                  eventSourceFactory: (url) =>
                    new FakeEventSource(url) as unknown as EventSource,
                }}
              />
            }
          />
          <Route path="/sessions/:bundleId" element={<h1>final artifact page</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const source = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  return { ...rendered, source, client };
}

beforeEach(() => {
  FakeEventSource.instances.length = 0;
});

describe("live session view", () => {
  it("puts an unmissable incomplete banner above everything", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);

    const banner = screen.getByRole("region", { name: /live session status/i });
    expect(within(banner).getByText(/LIVE — INCOMPLETE/)).toBeInTheDocument();
    // The discrete standing is announced; the ticking as-of line is not, so a
    // screen reader is told the state without a per-second flood.
    expect(within(banner).getByRole("status")).toHaveTextContent(/has not closed/i);
    expect(within(banner).getByText(/journal record #1/)).toBeInTheDocument();
    // And the view describes itself by the banner, so the state is reachable
    // from anywhere inside it.
    const view = banner.parentElement as HTMLElement;
    expect(view).toHaveAttribute(
      "aria-describedby",
      within(banner).getByRole("status").id,
    );
  });

  it("renders every unknowable value as an explicit unknown with a reason", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);

    const unknowns = screen.getByRole("region", { name: /not knowable yet/i });
    for (const label of [
      "Session status",
      "Duration",
      "Turns",
      "Turn metrics, including p95 first-token",
      "Interruptions",
      "Derived analysis",
      "Diagnoses",
    ]) {
      expect(within(unknowns).getByText(label)).toBeInTheDocument();
    }
    expect(within(unknowns).getAllByText(/unknown/).length).toBeGreaterThan(0);
    expect(
      within(unknowns).getByText(/analysis binds to the digest of a finished artifact/i),
    ).toBeInTheDocument();
  });

  it("never renders a summary statistic while the session is open", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "record",
      {
        kind: "event",
        value: { event_name: "turn.start" },
        omissions: [],
        retained_classes: [],
      },
      `${JOURNAL}:2`,
    );

    // "p95 first-token" appears only as a named unknown, never as a value, and
    // no zero stands in for a measurement that has not been taken.
    const metrics = screen.getByText(/p95 first-token/i);
    expect(metrics.closest("div")).toHaveTextContent(/unknown/);
    expect(screen.queryByText("0ms")).not.toBeInTheDocument();
    expect(screen.queryByText(/p95 first-token\s*0/)).not.toBeInTheDocument();
  });

  it("draws an unfinished operation as having no observed end", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "operation_open",
      {
        operation_id: "op-1",
        operation_name: "llm",
        turn_id: "turn-0",
        started_at: { monotonic_time_nano: "2000", clock_domain_id: "proc" },
        status: "unknown",
        ended_at: null,
        duration_nano: null,
        end_observed: false,
      },
      `${JOURNAL}:2`,
    );

    const panel = screen.getByRole("region", { name: /operations in progress/i });
    expect(within(panel).getByText("llm")).toBeInTheDocument();
    expect(within(panel).getByText("no end observed")).toBeInTheDocument();
    // No duration anywhere: not a number, not a dash, not a ticking clock.
    expect(within(panel).queryByText(/ms$/)).not.toBeInTheDocument();
  });

  it("retires an operation from the in-progress list once its record arrives", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "operation_open",
      {
        operation_id: "op-1",
        operation_name: "llm",
        started_at: { monotonic_time_nano: "2000" },
      },
      `${JOURNAL}:2`,
    );
    source.emit(
      "record",
      {
        kind: "operation",
        value: { operation_id: "op-1", operation_name: "llm", status: "ok" },
        omissions: [],
        retained_classes: [],
      },
      `${JOURNAL}:3`,
    );

    const panel = screen.getByRole("region", { name: /operations in progress/i });
    expect(within(panel).queryByText("no end observed")).not.toBeInTheDocument();
    expect(
      within(panel).getByText(/not a claim that none is running/i),
    ).toBeInTheDocument();
  });

  it("states that a close is not an artifact, and waits for one", () => {
    const { source } = renderLive({ incidents: { items: [], next_cursor: null } });
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "finalize",
      {
        status: "completed",
        ended_at: { monotonic_time_nano: "9000" },
        journal_complete: true,
        first_limit_reason: null,
        truncated_records: 0,
        artifact_available: false,
      },
      `${JOURNAL}:2`,
    );

    expect(screen.getByText(/CLOSED — ARTIFACT NOT YET STORED/)).toBeInTheDocument();
    const panel = screen.getByRole("region", { name: /final artifact/i });
    expect(
      within(panel).getByText(/Waiting for the immutable artifact/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /show the final artifact/i })).toBeNull();
  });

  it("hands over to the artifact only through an explicit action", () => {
    const { source } = renderLive({
      incidents: {
        items: [{ bundle_id: "b-live", session_id: "s-live", finality: "final" }],
        next_cursor: null,
      },
    });
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "finalize",
      {
        status: "completed",
        ended_at: { monotonic_time_nano: "9000" },
        journal_complete: true,
        first_limit_reason: null,
        truncated_records: 0,
        artifact_available: false,
      },
      `${JOURNAL}:2`,
    );

    // The live view is never silently upgraded in place; the reader chooses.
    const link = screen.getByRole("link", { name: /show the final artifact/i });
    expect(screen.getByText(/CLOSED — ARTIFACT NOT YET STORED/)).toBeInTheDocument();
    fireEvent.click(link);
    expect(
      screen.getByRole("heading", { name: /final artifact page/i }),
    ).toBeInTheDocument();
  });

  it("says when the stream ended without a close record", () => {
    const { source } = renderLive({ incidents: { items: [], next_cursor: null } });
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit("end", { reason: "journal_removed", close_observed: false });

    expect(screen.getByText(/STREAM ENDED — NO CLOSE RECORD/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/evidence may be incomplete/i);
    expect(
      screen.getByText(/one can be recovered from its checkpoint journal/i),
    ).toBeInTheDocument();
  });

  it("declares withheld replay and journal exhaustion rather than hiding them", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit("replay_truncated", {
      reason: "replay_window_exceeded",
      withheld_records: 12,
      available_from_sequence: 14,
    });
    source.emit("exhausted", { reason: "checkpoint_journal_full" }, `${JOURNAL}:2`);

    const banner = screen.getByRole("region", { name: /live session status/i });
    expect(within(banner).getByText(/12 earlier records exist/i)).toBeInTheDocument();
    expect(within(banner).getByText(/journal reached its cap/i)).toBeInTheDocument();
  });

  it("offers a keyboard-reachable reconnect after the connection is closed", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    for (let attempt = 0; attempt < 5; attempt += 1) source.fail();

    const panel = screen.getByRole("region", { name: /^connection$/i });
    const button = within(panel).getByRole("button", { name: /reconnect and catch up/i });
    button.focus();
    expect(button).toHaveFocus();
    expect(within(panel).getByText(/nothing was dropped/i)).toBeInTheDocument();

    fireEvent.click(button);
    // A second EventSource means the tail really re-subscribed, and the browser
    // resumes it with Last-Event-ID.
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("discards everything on reset so two journals never splice together", () => {
    const { source } = renderLive();
    source.emit("open", OPEN_PAYLOAD, `${JOURNAL}:1`);
    source.emit(
      "record",
      {
        kind: "participant",
        value: { participant_id: "p" },
        omissions: [],
        retained_classes: [],
      },
      `${JOURNAL}:2`,
    );
    expect(screen.getByText("participant")).toBeInTheDocument();

    source.emit("reset", {
      reason: "journal_identity_changed",
      previous_journal_id: JOURNAL,
      journal_id: "another",
    });

    expect(screen.queryByText("participant")).not.toBeInTheDocument();
    expect(screen.getByText(/No records have been admitted yet/i)).toBeInTheDocument();
  });
});
