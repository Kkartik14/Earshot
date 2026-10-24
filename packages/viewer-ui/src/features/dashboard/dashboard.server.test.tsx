import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { join } from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ViewerQueryScopeProvider } from "../../api/queryScope";
import { ObserveFeature } from "../../ObserveFeature";
import { startRealServer, type RealServer } from "../../test/realServer";

// Exercise the dashboard over real HTTP with a projected Pipecat capture and seeded call states.

const SCRIPT = join(import.meta.dirname, "__fixtures__", "serve_dashboard.py");
const WAIT = { timeout: 15_000 };

let server: RealServer<{ port: number; now_ms: number }>;

beforeAll(async () => {
  server = await startRealServer(SCRIPT);
}, 120_000);

afterAll(async () => {
  await server?.stop();
  vi.restoreAllMocks();
}, 60_000);

beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(server.seed.now_ms);
});

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ViewerQueryScopeProvider
        scope={{ projectId: "default", authContextId: "server-test" }}
      >
        <ObserveFeature route={{ kind: "dashboard" }} currentPath="/dashboard" />
      </ViewerQueryScopeProvider>
    </QueryClientProvider>,
  );
}

const tile = (name: string) => screen.getByRole("region", { name });
const rows = (table: HTMLElement) => within(table).getAllByRole("row").slice(1);
const cells = (row: HTMLElement) =>
  [...within(row).queryAllByRole("rowheader"), ...within(row).getAllByRole("cell")].map(
    (cell) => cell.textContent,
  );

describe("dashboard on a real server", () => {
  it("shows each tile with the turns and calls it covers", async () => {
    renderDashboard();
    await screen.findByRole("region", { name: "Calls" }, WAIT);

    expect(tile("Calls")).toHaveTextContent("4");
    expect(tile("Calls")).toHaveTextContent("1 more received, not projected yet");
    // Percentiles over the six reported turns, not over each call's median.
    expect(tile("Reported latency")).toHaveTextContent("p50 1.10 s");
    expect(tile("Reported latency")).toHaveTextContent("p95 1.80 s");
    expect(tile("Reported latency")).toHaveTextContent("6 of 7 turns · 3 of 4 calls");
    expect(tile("Audible gap")).toHaveTextContent("p50 1.00 s");
    expect(tile("Audible gap")).toHaveTextContent("p95 1.20 s");
    expect(tile("Audible gap")).toHaveTextContent("2 of 7 turns · 1 of 4 calls");
    expect(tile("Interruption rate")).toHaveTextContent("66.7 per 100 turns");
    expect(tile("Interruption rate")).toHaveTextContent("2 of 4 calls");
    expect(tile("Dead air")).toHaveTextContent("1.7% of call time");
    expect(tile("Dead air")).toHaveTextContent("2 of 4 calls");
  });

  it("shows Earshot's and the provider's cost side by side, never summed", async () => {
    renderDashboard();
    const cost = await screen.findByRole("region", { name: "Cost per minute" }, WAIT);

    const earshot = within(cost).getByRole("group", { name: "Earshot estimate" });
    const provider = within(cost).getByRole("group", { name: "Provider reported" });
    expect(earshot).toHaveTextContent("$0.06/min");
    expect(earshot).toHaveTextContent("2 of 4 calls with a complete cost");
    expect(provider).toHaveTextContent("$0.09/min");
    expect(provider).toHaveTextContent("2 of 4 calls");
    expect(provider).toHaveTextContent("retell.combined_cost, vapi.cost");
    expect(cost).not.toHaveTextContent("$0.15");
  });

  it("lists the LLM's first-token latency by provider and basis", async () => {
    renderDashboard();
    const table = await screen.findByRole("table", { name: /LLM first token/ }, WAIT);

    expect(table).toHaveAccessibleName("LLM first token by provider (1 of 4 calls)");
    expect(rows(table).map(cells)).toEqual([
      ["groq", "provider stage direct", "0.62 s", "1.11 s", "3", "1"],
    ]);
  });

  it("splits the calls by agent when asked", async () => {
    renderDashboard();
    await screen.findByRole("table", { name: "By framework" }, WAIT);
    expect(rows(screen.getByRole("table", { name: "By framework" })).map(cells)).toEqual([
      [
        "pipecat",
        "4",
        "1.10 s / 1.80 s",
        "1.20 s",
        "66.7 per 100 turns",
        "1.7%",
        "$0.06/min",
        "$0.09/min",
      ],
    ]);

    fireEvent.change(screen.getByLabelText("Group by"), { target: { value: "agent" } });

    const byAgent = await screen.findByRole("table", { name: "By agent" }, WAIT);
    expect(rows(byAgent).map((row) => cells(row).slice(0, 2))).toEqual([
      ["support-agent", "2"],
      ["earshot-pipecat-otlp-example", "1"],
      ["unknown", "1"],
    ]);
  });

  it("draws calls per bucket with a table of the same numbers", async () => {
    renderDashboard();
    const chart = await screen.findByRole("img", { name: /^Calls per hour/ }, WAIT);

    expect(chart).toHaveAccessibleName(expect.stringMatching(/4 calls in 2[45] hours$/));
    const toggle = screen.getByRole("button", { name: "Show as table" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const table = screen.getByRole("table", { name: "Calls per hour (UTC)" });
    const counts = rows(table).map((row) => Number(cells(row)[1]));
    expect(counts.reduce((sum, value) => sum + value, 0)).toBe(4);

    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "7d" } });
    expect(
      await screen.findByRole("img", { name: /^Calls per day/ }, WAIT),
    ).toHaveAccessibleName(expect.stringMatching(/4 calls in [78] days$/));
  });

  it("is reachable from the rail and labels every control", async () => {
    renderDashboard();
    await screen.findByRole("region", { name: "Calls" }, WAIT);

    expect(
      screen.getByRole("heading", { level: 1, name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "href",
      "/dashboard",
    );
    for (const label of ["Range", "Bucket", "Group by"]) {
      expect(screen.getByRole("combobox", { name: label })).toBeInTheDocument();
    }
  });
});
