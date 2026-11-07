/// <reference types="node" />
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { configureApiBasePath } from "../../api/client";
import { ViewerQueryScopeProvider, viewerQueryKey } from "../../api/queryScope";
import type { ObserveRoute } from "../../navigation";
import { ObserveFeature } from "../../ObserveFeature";
import type { CallDetail, CallLiveTurnsCache } from "./api";
import { fakeClocks } from "./__fixtures__/fakeMediaClock";
import { MediaClockContext } from "./Player";

// Exercise the calls UI over real HTTP with real server data; jsdom uses a fake media clock.

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
let commandId = 0;
const commandWaiters = new Map<
  number,
  {
    resolve: (result: Record<string, unknown>) => void;
    timeout: ReturnType<typeof setTimeout>;
  }
>();
const observedLiveTurnFetches: {
  url: string;
  requestHeaders: Headers;
  status: number;
  etag: string | null;
}[] = [];

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
    let receivedSeed = false;
    lines.on("line", (line) => {
      const message = JSON.parse(line) as Record<string, unknown>;
      if (!receivedSeed) {
        receivedSeed = true;
        ready(message as unknown as Seed);
        return;
      }
      const id = message.command_id;
      if (typeof id !== "number") return;
      const waiter = commandWaiters.get(id);
      if (waiter == null) return;
      clearTimeout(waiter.timeout);
      commandWaiters.delete(id);
      waiter.resolve(message);
    });
    server.once("exit", (code) =>
      fail(new Error(`serve_calls.py exited (${code}) before it was ready:\n${stderr}`)),
    );
  });
}

function sendServerCommand<T extends Record<string, unknown>>(
  command: Record<string, string | number>,
): Promise<T> {
  const id = ++commandId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      commandWaiters.delete(id);
      reject(new Error(`serve_calls.py did not acknowledge command ${id}`));
    }, 10_000);
    commandWaiters.set(id, {
      resolve: (result) => resolve(result as T),
      timeout,
    });
    server.stdin.write(`${JSON.stringify({ ...command, command_id: id })}\n`);
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
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = absolute(input);
    return nodeFetch(url, init).then((response) => {
      const parsed = new URL(url instanceof Request ? url.url : url.toString());
      if (parsed.pathname.endsWith("/live-turns")) {
        observedLiveTurnFetches.push({
          url: parsed.href,
          requestHeaders: new Headers(init?.headers),
          status: response.status,
          etag: response.headers.get("ETag"),
        });
      }
      return response;
    });
  }) as typeof fetch;
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
  return { clocks, client };
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
    const { clocks } = renderObserve(
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
    // Turn 2 as the projection job measures it (the golden: 1.510 s ± 75 ms).
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

  it("keeps live turns visible through close, then settles on the durable projection", async () => {
    observedLiveTurnFetches.length = 0;
    const { client } = renderObserve(
      { kind: "call", callId: seed.livekit_call_id },
      `/calls/${seed.livekit_call_id}`,
    );
    const scope = { projectId: "default", authContextId: "server-test" };
    const liveKey = viewerQueryKey(scope, ["live-call-turns", seed.livekit_call_id]);
    const callKey = viewerQueryKey(scope, ["call", seed.livekit_call_id]);
    const table = await screen.findByRole("table", { name: /reported latency/i }, WAIT);
    expect(within(table).getAllByRole("row").length).toBeGreaterThan(1);
    expect(client.getQueryData<CallLiveTurnsCache>(liveKey)?.body.state).toBe("open");
    await waitFor(() => {
      expect(observedLiveTurnFetches.some((item) => item.status === 304)).toBe(true);
    }, WAIT);
    const liveTurnFetches = observedLiveTurnFetches.filter((item) =>
      item.url.endsWith(`/v1/calls/${seed.livekit_call_id}/live-turns`),
    );
    const initial = liveTurnFetches.find((item) => item.status === 200);
    const unchanged = liveTurnFetches.find((item) => item.status === 304);
    expect(initial?.etag).not.toBeNull();
    expect(unchanged?.requestHeaders.get("If-None-Match")).toBe(initial?.etag);

    await act(async () => {
      server.stdin.write(
        `${JSON.stringify({ action: "close_call", call_id: seed.livekit_call_id })}\n`,
      );
      await expect
        .poll(() => client.getQueryData<CallDetail>(callKey), { timeout: 120_000 })
        .toMatchObject({ state: "closed", projection_state: "none" });
    });
    const liveTurns = client.getQueryData<CallLiveTurnsCache>(liveKey)?.body;
    expect(liveTurns?.state).toBe("closed");
    expect(liveTurns?.turns.length).toBeGreaterThan(0);
    expect(
      within(screen.getByRole("table", { name: /reported latency/i })).getAllByRole("row")
        .length,
    ).toBeGreaterThan(1);

    const closedCache = client.getQueryData<CallLiveTurnsCache>(liveKey);
    const closedLiveFetchCount = observedLiveTurnFetches.filter((item) =>
      item.url.endsWith(`/v1/calls/${seed.livekit_call_id}/live-turns`),
    ).length;
    const liveUpdatedAt = client.getQueryState(liveKey)?.dataUpdatedAt;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    const pendingProjectionFetches = observedLiveTurnFetches.filter((item) =>
      item.url.endsWith(`/v1/calls/${seed.livekit_call_id}/live-turns`),
    );
    expect(pendingProjectionFetches).toHaveLength(closedLiveFetchCount + 1);
    expect(pendingProjectionFetches.at(-1)?.status).toBe(304);
    expect(pendingProjectionFetches.at(-1)?.requestHeaders.get("If-None-Match")).toBe(
      closedCache?.etag,
    );
    expect(client.getQueryData<CallLiveTurnsCache>(liveKey)?.body).toEqual(
      closedCache?.body,
    );
    expect(client.getQueryState(liveKey)?.dataUpdatedAt).toBeGreaterThanOrEqual(
      liveUpdatedAt ?? 0,
    );

    await act(async () => {
      server.stdin.write(
        `${JSON.stringify({ action: "allow_projection", call_id: seed.livekit_call_id })}\n`,
      );
      await expect
        .poll(() => client.getQueryData<CallDetail>(callKey), { timeout: 120_000 })
        .toMatchObject({ state: "closed", projection_state: "current" });
    });
    const durable = client.getQueryData<CallDetail>(callKey);
    expect(durable?.projection?.turns).toEqual(liveTurns?.turns);
    expect(screen.getByRole("table", { name: /reported latency/i })).toBeInTheDocument();

    const settledLiveFetchCount = observedLiveTurnFetches.filter((item) =>
      item.url.endsWith(`/v1/calls/${seed.livekit_call_id}/live-turns`),
    ).length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(
      observedLiveTurnFetches.filter((item) =>
        item.url.endsWith(`/v1/calls/${seed.livekit_call_id}/live-turns`),
      ),
    ).toHaveLength(settledLiveFetchCount);
  }, 60_000);

  it("renders synthetic host-profile turns within the two-second polling budget", async () => {
    observedLiveTurnFetches.length = 0;
    const { host_profile_call_id: callId } = await sendServerCommand<{
      host_profile_call_id: string;
    }>({ action: "open_host_profile_call" });
    seed.call_count += 1;
    const { client } = renderObserve({ kind: "call", callId }, `/calls/${callId}`);
    const scope = { projectId: "default", authContextId: "server-test" };
    const liveKey = viewerQueryKey(scope, ["live-call-turns", callId]);

    await waitFor(() => {
      expect(client.getQueryData<CallLiveTurnsCache>(liveKey)?.body).toMatchObject({
        state: "open",
        turns: [],
      });
    }, WAIT);
    await waitFor(() => {
      expect(
        observedLiveTurnFetches.some(
          (item) =>
            item.url.endsWith(`/v1/calls/${callId}/live-turns`) && item.status === 200,
        ),
      ).toBe(true);
    }, WAIT);

    const emittedAt: number[] = [];
    await act(async () => {
      for (let number = 1; number <= 20; number += 1) {
        emittedAt.push(performance.now());
        const result = await sendServerCommand<{ accepted: boolean }>({
          action: "emit_host_turn",
          number,
        });
        expect(result.accepted).toBe(true);
      }
    });

    await waitFor(
      () => {
        const table = screen.getByRole("table", { name: /reported latency/i });
        expect(within(table).getAllByRole("row")).toHaveLength(21);
        expect(client.getQueryData<CallLiveTurnsCache>(liveKey)?.body.turns).toHaveLength(
          20,
        );
      },
      { timeout: 5_000 },
    );
    const visibleAt = performance.now();
    const visibilityMs = emittedAt.map((time) => visibleAt - time);
    const ordered = [...visibilityMs].sort((left, right) => left - right);
    const p95 = ordered[Math.ceil(0.95 * ordered.length) - 1];
    console.info(
      `E-3 synthetic React viewer: 20 host turns, 2 s polling, p95 ${p95.toFixed(2)} ms, max ${Math.max(...visibilityMs).toFixed(2)} ms`,
    );
    expect(p95).toBeLessThanOrEqual(5_000);
  }, 15_000);

  it("says a call is not found rather than showing an empty call", async () => {
    renderObserve(
      { kind: "call", callId: "call_0123456789abcdef0123456789abcdef" },
      "/calls/call_0123456789abcdef0123456789abcdef",
    );
    expect(await screen.findByText("Call not found", {}, WAIT)).toBeInTheDocument();
  });
});
