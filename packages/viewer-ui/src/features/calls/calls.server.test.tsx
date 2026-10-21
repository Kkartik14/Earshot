/// <reference types="node" />
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { configureApiBasePath } from "../../api/client";
import { ViewerQueryScopeProvider } from "../../api/queryScope";
import type { ObserveRoute } from "../../navigation";
import { ObserveFeature } from "../../ObserveFeature";
import { fakeClocks } from "./__fixtures__/fakeMediaClock";
import { MediaClockContext } from "./Player";

// The calls list and the call view against a real Earshot server
// (`__fixtures__/serve_calls.py`: the real app over real SQLite, with the two real
// captures, their recordings analysed by the server's own job, and 55 more calls).
// Every request is real HTTP; no hook or response is stubbed. The media clock is
// the declared fake, since jsdom cannot play audio.

interface Seed {
  port: number;
  pipecat_call_id: string;
  livekit_call_id: string;
  complete_cost_call_id: string;
  call_count: number;
  llm_span: { trace_id: string; span_id: string };
}

const ROOT = resolve(import.meta.dirname, "../../../../..");
const SCRIPT = join(import.meta.dirname, "__fixtures__", "serve_calls.py");
const START_TIMEOUT_MS = 120_000;
const WAIT = { timeout: 15_000 };

let server: ChildProcessWithoutNullStreams;
let seed: Seed;
let restoreGlobals: () => void;

/** The repo's venv (`.venv`, where the dev setup and CI install Earshot), or the
 * interpreter `EARSHOT_PYTHON` names. Turborepo's strict env mode does not pass
 * that variable to `turbo run test`, so it is for running vitest directly. */
function python(): string {
  const configured = process.env.EARSHOT_PYTHON;
  if (configured) return configured;
  const venv = join(ROOT, ".venv", "bin", "python");
  if (!existsSync(venv)) {
    throw new Error(
      `No Python at ${venv}: create it and install Earshot ('python -m venv .venv && .venv/bin/pip install -e ".[dev]"'), or set EARSHOT_PYTHON.`,
    );
  }
  return venv;
}

function startServer(): Promise<Seed> {
  server = spawn(python(), [SCRIPT], { cwd: ROOT, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  server.stderr.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  return new Promise((ready, fail) => {
    const lines = createInterface({ input: server.stdout });
    lines.once("line", (line) => ready(JSON.parse(line) as Seed));
    server.once("exit", (code) =>
      fail(new Error(`serve_calls.py exited (${code}) before it was ready:\n${stderr}`)),
    );
  });
}

/** A browser resolves a relative request URL against the page; jsdom runs on
 * Node's fetch, which does not. Point the page at the real server and resolve
 * relative URLs against it, as a browser on that origin would. */
function browseFrom(origin: string): () => void {
  (
    globalThis as unknown as { jsdom: { reconfigure(options: { url: string }): void } }
  ).jsdom.reconfigure({
    url: origin,
  });
  const NodeRequest = globalThis.Request;
  const nodeFetch = globalThis.fetch;
  const absolute = (input: RequestInfo | URL) =>
    typeof input === "string" ? new URL(input, document.baseURI).href : input;
  class PageRequest extends NodeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(absolute(input), init);
    }
  }
  globalThis.Request = PageRequest as typeof Request;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    nodeFetch(absolute(input), init)) as typeof fetch;
  // The API client captures fetch and Request when it is made: remake it.
  configureApiBasePath("");
  return () => {
    globalThis.Request = NodeRequest;
    globalThis.fetch = nodeFetch;
    configureApiBasePath("");
  };
}

beforeAll(async () => {
  seed = await startServer();
  restoreGlobals = browseFrom(`http://127.0.0.1:${seed.port}/`);
}, START_TIMEOUT_MS);

afterAll(async () => {
  restoreGlobals?.();
  if (server == null || server.exitCode != null) return;
  const exited = new Promise((done) => server.once("exit", done));
  server.stdin.end();
  const timer = setTimeout(() => server.kill("SIGKILL"), 30_000);
  await exited;
  clearTimeout(timer);
}, 60_000);

function renderObserve(route: ObserveRoute, currentPath: string) {
  const { factory, clocks } = fakeClocks();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ViewerQueryScopeProvider
        scope={{ projectId: "default", authContextId: "server-test" }}
      >
        <MediaClockContext.Provider value={factory}>
          <ObserveFeature route={route} currentPath={currentPath} />
        </MediaClockContext.Provider>
      </ViewerQueryScopeProvider>
    </QueryClientProvider>,
  );
  return clocks;
}

const callsTable = () => screen.getByRole("table", { name: /calls, newest first/i });
const bodyRows = () => within(callsTable()).getAllByRole("row").slice(1);

function filterBy(values: Record<string, string>) {
  const form = screen.getByRole("search", { name: "Filter calls" });
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(within(form).getByLabelText(label), { target: { value } });
  }
  fireEvent.click(within(form).getByRole("button", { name: "Apply filters" }));
}

describe("calls list on a real server", () => {
  it("lists every call, newest first, a page at a time", async () => {
    renderObserve({ kind: "calls" }, "/calls");

    expect(
      await screen.findByText("Showing 50 calls; more are available.", {}, WAIT),
    ).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(
      await screen.findByText(`Showing ${seed.call_count} calls.`, {}, WAIT),
    ).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(seed.call_count);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    // A call that is not projected shows no metrics rather than zeros. The server
    // projects every closed call, so these are the calls still open.
    expect(screen.getAllByText(/^not projected yet/).length).toBe(seed.call_count - 2);
    // The rail links the calls with sessions and fleet metrics.
    expect(screen.getByRole("link", { name: "Calls" })).toHaveAttribute("href", "/calls");
  });

  it("filters on the server by agent, tag and transcript text", async () => {
    renderObserve({ kind: "calls" }, "/calls");
    await screen.findByText(/Showing 50 calls/, {}, WAIT);

    filterBy({
      Agent: "booking-agent",
      "Tags (all of)": "vip, refund",
      "Transcript text": "window",
    });

    expect(await screen.findByText("Showing 1 call.", {}, WAIT)).toBeInTheDocument();
    const [row] = bodyRows();
    expect(within(row).getByRole("link")).toHaveAttribute(
      "href",
      `/calls/${seed.pipecat_call_id}`,
    );
    expect(row).toHaveTextContent("booking-agent");
    expect(row).toHaveTextContent("≥ $0.00367 · 2 unknown");
    expect(row).toHaveTextContent("+ ~$0.00417 (estimated)");
  });

  it("caps cost on complete costs only and counts the calls it left out", async () => {
    renderObserve({ kind: "calls" }, "/calls");
    await screen.findByText(/Showing 50 calls/, {}, WAIT);

    filterBy({ "Cost at most": "0.01" });

    expect(await screen.findByText("Showing 1 call.", {}, WAIT)).toBeInTheDocument();
    expect(within(bodyRows()[0]).getByRole("link")).toHaveAttribute(
      "href",
      `/calls/${seed.complete_cost_call_id}`,
    );
    expect(screen.getByText("1 call with incomplete cost not shown")).toBeInTheDocument();
  });

  it("says when nothing matches, and explains a bad filter in place", async () => {
    renderObserve({ kind: "calls" }, "/calls");
    await screen.findByText(/Showing 50 calls/, {}, WAIT);

    filterBy({ "Transcript text": "ab" });
    const text = screen.getByLabelText("Transcript text");
    expect(text).toHaveAttribute("aria-invalid", "true");
    expect(text).toHaveAccessibleDescription("Enter at least 3 characters.");
    expect(bodyRows()).toHaveLength(50);

    filterBy({ "Transcript text": "", Agent: "nobody-by-this-name" });
    expect(
      await screen.findByText("No calls match these filters.", {}, WAIT),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("call view on a real server", () => {
  it("plays the rendition with the server's peaks, one lane per speaker", async () => {
    const clocks = renderObserve(
      { kind: "call", callId: seed.pipecat_call_id },
      `/calls/${seed.pipecat_call_id}`,
    );
    await screen.findByRole("heading", { name: /^Call pipecat-otlp-/ }, WAIT);
    await screen.findByRole("button", { name: "Play" }, WAIT);
    await expect.poll(() => clocks.length, WAIT).toBe(1);

    const source = clocks[0].source;
    expect(source.url).toBe("/v1/recordings/pipecat.user/rendition");
    expect(source.lanes).toEqual(["user", "agent"]);
    // The real rendition peaks: the analysis job's two mono files side by side.
    expect(source.peaks?.map((lane) => lane.length)).toEqual([2 * 2031, 2 * 2031]);
    expect(screen.getByText("≤ 3.08 s")).toBeInTheDocument();
    // Turn 2 as the projection job measures it (goldens.md: 1.510 s ± 75 ms).
    expect(screen.getByText("1.51 s ± 0.08")).toBeInTheDocument();
  });

  it("opens the real stored llm span of a turn in a drawer", async () => {
    renderObserve(
      { kind: "call", callId: seed.pipecat_call_id },
      `/calls/${seed.pipecat_call_id}`,
    );
    const turns = await screen.findByRole("table", { name: /reported latency/i }, WAIT);
    const firstTurn = within(turns).getAllByRole("row")[1];
    fireEvent.click(within(firstTurn).getByRole("button", { name: "LLM llm" }));

    const drawer = screen.getByRole("dialog", { name: "llm span detail" });
    expect(
      await within(drawer).findByText(
        "Sure, what time would you like to come in tomorrow evening?",
        {},
        WAIT,
      ),
    ).toBeInTheDocument();
    expect(drawer).toHaveTextContent(
      `${seed.llm_span.trace_id}/${seed.llm_span.span_id}`,
    );
  });

  it("shows an unprojected call's span transcript and recording", async () => {
    renderObserve(
      { kind: "call", callId: seed.livekit_call_id },
      `/calls/${seed.livekit_call_id}`,
    );

    expect(
      await screen.findByRole("heading", { name: "Transcript from spans" }, WAIT),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/I would like to book a table for two people/),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recordings" })).toHaveTextContent(
      "livekit.stereo · user + agent · stored",
    );
    expect(screen.queryByRole("button", { name: "Play" })).toBeNull();
  });

  it("says a call is not found rather than showing an empty call", async () => {
    renderObserve(
      { kind: "call", callId: "call_0123456789abcdef0123456789abcdef" },
      "/calls/call_0123456789abcdef0123456789abcdef",
    );
    expect(await screen.findByText("Call not found", {}, WAIT)).toBeInTheDocument();
  });
});
