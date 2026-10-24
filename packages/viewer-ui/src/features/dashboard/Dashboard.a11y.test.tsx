import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DashboardBody, DashboardTiles } from "./api";
import { DashboardView } from "./Dashboard";

// What the dashboard shows when the server knows nothing: bodies in the
// `GET /v1/dashboard` shape (the generated schema types them) for the cases the
// real-server suite cannot reach with its data. An unknown is a dash and names
// its coverage; it is never rendered as 0.

const HOUR = 3_600_000_000_000n;
const START = 1_800_000_000_000_000_000n;

function unmeasured(calls: number): DashboardTiles {
  const latency = {
    p50_ms: null,
    p95_ms: null,
    turns: 0,
    of_turns: 0,
    calls: 0,
    of_calls: calls,
  };
  const rate = {
    nano_usd_per_minute: null,
    cost_nano_usd: null,
    duration_ms: null,
    calls: 0,
    of_calls: calls,
  };
  return {
    calls,
    reported_latency: latency,
    audible_gap: latency,
    llm_first_token: { providers: [], calls: 0, of_calls: calls },
    interruption_rate: { barge_ins: null, turns: 0, calls: 0, of_calls: calls },
    dead_air: { dead_air_ms: null, duration_ms: null, calls: 0, of_calls: calls },
    cost_per_minute: { earshot: rate, provider: { ...rate, bases: [] } },
  };
}

function body(calls: number, notProjected: number): DashboardBody {
  return {
    from_unix_nano: String(START),
    to_unix_nano: String(START + 2n * HOUR),
    bucket: "hour",
    group_by: "agent",
    not_projected_calls: notProjected,
    overall: unmeasured(calls),
    buckets: [
      { start_unix_nano: String(START), tiles: unmeasured(calls) },
      { start_unix_nano: String(START + HOUR), tiles: unmeasured(0) },
    ],
    groups: calls === 0 ? [] : [{ value: null, tiles: unmeasured(calls) }],
  };
}

describe("dashboard with nothing measured", () => {
  it("says the range holds no projected call, and how many wait for projection", () => {
    render(<DashboardView body={body(0, 2)} />);

    expect(screen.getByText("No projected calls in this range")).toBeInTheDocument();
    expect(
      screen.getByText("2 received in this range are not projected yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("shows unknown tiles as dashes with their coverage, never as zeros", () => {
    render(<DashboardView body={body(2, 0)} />);

    for (const name of ["Reported latency", "Audible gap"]) {
      const tile = screen.getByRole("region", { name });
      expect(tile).toHaveTextContent("p50 —");
      expect(tile).toHaveTextContent("p95 —");
      expect(tile).toHaveTextContent("no turns · 0 of 2 calls");
    }
    expect(screen.getByRole("region", { name: "Interruption rate" })).toHaveTextContent(
      /^Interruption rate—barge-ins · 0 of 2 calls/,
    );
    expect(screen.getByRole("region", { name: "Dead air" })).toHaveTextContent(
      /^Dead air—0 of 2 calls/,
    );
    const cost = screen.getByRole("region", { name: "Cost per minute" });
    for (const name of ["Earshot estimate", "Provider reported"]) {
      expect(within(cost).getByRole("group", { name })).toHaveTextContent(
        /—0 of 2 calls/,
      );
    }
    expect(screen.queryByText(/\$0|0 ms|0%|0\.00 s/)).toBeNull();
    // A group whose value is unknown is named so, never left blank.
    const groups = screen.getByRole("table", { name: "By agent" });
    expect(
      within(groups).getByRole("rowheader", { name: "unknown" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "LLM first token by provider (0 of 2 calls)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Calls per hour: 2 calls in 2 hours" }),
    ).toBeInTheDocument();
  });
});
