import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { ViewerQueryScopeProvider, viewerQueryKey } from "../../api/queryScope";
import callDetailFixture from "./__fixtures__/call-detail.json";
import spanFixture from "./__fixtures__/llm-span.json";
import { fakeClocks } from "./__fixtures__/fakeMediaClock";
import { CallView } from "./CallView";
import {
  useCall,
  useLiveCallTurns,
  type CallDetail,
  type CallProjection,
  type CallSpan,
} from "./api";
import { MediaClockContext } from "./Player";

afterEach(() => {
  vi.restoreAllMocks();
});

function LiveTurnsProbe() {
  const query = useLiveCallTurns("call_0123456789abcdef0123456789abcdef", "open");
  return (
    <output>
      {query.data == null
        ? query.isError
          ? "error"
          : "pending"
        : `${query.data.state}:${query.data.turns.length}`}
    </output>
  );
}

function ClosedCallProbe() {
  const call = useCall("call_0123456789abcdef0123456789abcdef");
  const live = useLiveCallTurns(
    "call_0123456789abcdef0123456789abcdef",
    call.data?.state,
    call.data?.projection_state,
  );
  return (
    <output>
      {`${call.data?.state ?? "pending"}:${live.data?.state ?? "pending"}:${live.data?.revision ?? "none"}`}
    </output>
  );
}

function CallQueryProbe() {
  const query = useCall("call_0123456789abcdef0123456789abcdef");
  return <output>{query.isError ? "error" : "pending"}</output>;
}

function CallAnalysisProbe() {
  const query = useCall("call_0123456789abcdef0123456789abcdef");
  const data = query.data;
  return (
    <output>
      {data == null
        ? "pending"
        : `${data.projection_state}:${data.projection?.analysis_state ?? "none"}`}
    </output>
  );
}

describe("live call turn query", () => {
  it("does not retry permanent live-turn errors with the browser QueryClient defaults", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } },
    });
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "FORBIDDEN" } }), { status: 403 }),
    );

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <LiveTurnsProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("error")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not retry permanent Call detail errors with the browser QueryClient defaults", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } },
    });
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValue({
      error: { error: { code: "FORBIDDEN" } },
      response: new Response(null, { status: 403 }),
    } as never);

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <CallQueryProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("error")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("stops polling a closed detail after a permanent error", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } },
    });
    const base = callDetailFixture as CallDetail;
    const scope = { projectId: "default", authContextId: "test" };
    client.setQueryData(viewerQueryKey(scope, ["call", base.call_id]), {
      ...base,
      state: "closed",
      projection_state: "stale",
    });
    const get = vi.spyOn(api, "GET").mockResolvedValue({
      error: { error: { code: "EARSHOT_CALL_NOT_FOUND" } },
      response: new Response(null, { status: 404 }),
    } as never);

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={scope}>
          <CallQueryProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("error")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(get).toHaveBeenCalledTimes(1);
  }, 10_000);

  it("clears the span drawer when navigating directly to another cached Call", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const liveTurns = (base.projection as CallProjection).turns;
    const first: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
      recordings: [],
    };
    const second: CallDetail = {
      ...first,
      call_id: "call_abcdef0123456789abcdef0123456789",
    };
    const scope = { projectId: "default", authContextId: "test" };
    client.setQueryData(viewerQueryKey(scope, ["call", first.call_id]), first);
    client.setQueryData(viewerQueryKey(scope, ["call", second.call_id]), second);
    const bodyFor = (call: CallDetail) => ({
      call_id: call.call_id,
      state: "open",
      revision: null,
      axis_origin_unix_nano: base.first_evidence_unix_nano,
      axis_origin: "first_evidence",
      turns: liveTurns,
    });
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const call = String(input).includes(second.call_id) ? second : first;
      return new Response(JSON.stringify(bodyFor(call)), {
        status: 200,
        headers: { ETag: `"${call.call_id}"` },
      });
    });
    vi.spyOn(api, "GET").mockResolvedValue({
      data: spanFixture as CallSpan,
      response: new Response("{}", { status: 200 }),
    } as never);
    const { factory } = fakeClocks();
    const view = render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={scope}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={first.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    fireEvent.click((await screen.findAllByRole("button", { name: "LLM llm" }))[0]);
    expect(
      await screen.findByRole("dialog", { name: "llm span detail" }),
    ).toBeInTheDocument();

    view.rerender(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={scope}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={second.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(fetcher).toHaveBeenCalled();
  });

  it("keeps polling while a current closed projection has pending analysis", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const currentPending: CallDetail = {
      ...base,
      state: "closed",
      projection_state: "current",
      projection: { ...base.projection!, analysis_state: "pending" },
    };
    const stalePending: CallDetail = {
      ...currentPending,
      projection_state: "stale",
    };
    const currentStored: CallDetail = {
      ...base,
      state: "closed",
      projection_state: "current",
      projection: { ...base.projection!, analysis_state: "stored" },
    };
    const get = vi.spyOn(api, "GET");
    get
      .mockResolvedValueOnce({
        data: currentPending,
        response: new Response("{}", { status: 200 }),
      } as never)
      .mockResolvedValueOnce({
        data: stalePending,
        response: new Response("{}", { status: 200 }),
      } as never)
      .mockResolvedValue({
        data: currentStored,
        response: new Response("{}", { status: 200 }),
      } as never);

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <CallAnalysisProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("current:pending")).toBeInTheDocument();
    expect(
      await screen.findByText("stale:pending", {}, { timeout: 4_000 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("current:stored", {}, { timeout: 4_000 }),
    ).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(get).toHaveBeenCalledTimes(3);
  }, 12_000);

  it("shows a notice when live-turn polling fails but Call detail is still available", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retryDelay: 0, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const call: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
      recordings: [],
    };
    const scope = { projectId: "default", authContextId: "test" };
    client.setQueryData(viewerQueryKey(scope, ["call", call.call_id]), call);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "TEMPORARY" } }), { status: 503 }),
    );

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={scope}>
          <CallView callId={call.call_id} />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText(
        "Live turn updates could not be refreshed; showing the latest available Call data.",
      ),
    ).toHaveAttribute("role", "status");
  });

  it("moves focus to the access denial after an open Call's span drawer is removed", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const call: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
    };
    const scope = { projectId: "default", authContextId: "test" };
    const callQueryKey = viewerQueryKey(scope, ["call", base.call_id]);
    client.setQueryData(callQueryKey, call);
    client.setQueryData(viewerQueryKey(scope, ["live-call-turns", base.call_id]), {
      body: {
        call_id: base.call_id,
        state: "open",
        revision: null,
        turns: base.projection?.turns ?? [],
      },
      etag: '"open-v1"',
    });
    vi.spyOn(api, "GET").mockImplementation(() => new Promise(() => undefined) as never);
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "FORBIDDEN" } }), { status: 403 }),
    );
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={scope}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    const spanButtons = await screen.findAllByRole("button", { name: "LLM llm" });
    fireEvent.click(spanButtons[0]);
    const close = await screen.findByRole("button", { name: "Close detail" });
    expect(close).toHaveFocus();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_100));
    });

    const heading = await screen.findByRole("heading", {
      name: "Call access unavailable",
    });
    expect(heading).toHaveFocus();
  }, 10_000);

  it.each([401, 403])(
    "stops live-turn polling after HTTP %i",
    async (status) => {
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: Infinity } },
      });
      const fetcher = vi.spyOn(globalThis, "fetch");
      fetcher.mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            call_id: "call_0123456789abcdef0123456789abcdef",
            state: "open",
            revision: null,
            turns: [],
          }),
          { status: 200, headers: { ETag: '"live-v1"' } },
        ),
      );
      fetcher.mockResolvedValue(
        new Response(JSON.stringify({ error: { code: "FORBIDDEN" } }), { status }),
      );

      render(
        <QueryClientProvider client={client}>
          <ViewerQueryScopeProvider
            scope={{ projectId: "default", authContextId: "test" }}
          >
            <LiveTurnsProbe />
          </ViewerQueryScopeProvider>
        </QueryClientProvider>,
      );

      expect(await screen.findByText("open:0")).toBeInTheDocument();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 2_200));
      });
      expect(fetcher).toHaveBeenCalledTimes(2);
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 2_200));
      });
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
    10_000,
  );

  it("retries transient live failures while the Call is open", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } },
    });
    const fetcher = vi.spyOn(globalThis, "fetch");
    for (let attempt = 0; attempt < 4; attempt += 1) {
      fetcher.mockResolvedValueOnce(new Response(null, { status: 503 }));
    }
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: "call_0123456789abcdef0123456789abcdef",
          state: "open",
          revision: null,
          turns: [],
        }),
        { status: 200, headers: { ETag: '"revision-1"' } },
      ),
    );
    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <LiveTurnsProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("error")).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(4);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });

    expect(await screen.findByText("open:0")).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(5);
  }, 10_000);

  it("retries a failed detail refresh until the newer Revision is current", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } },
    });
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const detail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...projection,
        revision: 1,
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const newerTurns = projection.turns.slice(0, 1);
    const updatedDetail: CallDetail = {
      ...detail,
      latest_revision: { ...detail.latest_revision!, revision: 2 },
      projection: { ...detail.projection!, revision: 2, turns: newerTurns },
    };
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: base.call_id,
          state: "closed",
          revision: 2,
          turns: newerTurns,
        }),
        { status: 200, headers: { ETag: '"revision-2"' } },
      ),
    );
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce({
      data: detail,
      response: new Response("{}", { status: 200 }),
    } as never);
    for (let attempt = 0; attempt < 4; attempt += 1) {
      get.mockRejectedValueOnce(new Error("detail refresh temporarily failed"));
    }
    get.mockResolvedValue({
      data: updatedDetail,
      response: new Response("{}", { status: 200 }),
    } as never);
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    const table = await screen.findByRole("table", { name: /reported latency/i });
    expect(within(table).getAllByRole("row")).toHaveLength(newerTurns.length + 1);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(6);
    expect(
      within(screen.getByRole("table", { name: /reported latency/i })).getAllByRole(
        "row",
      ),
    ).toHaveLength(newerTurns.length + 1);
    await waitFor(
      () => {
        expect(
          client.getQueryData<CallDetail>(
            viewerQueryKey({ projectId: "default", authContextId: "test" }, [
              "call",
              base.call_id,
            ]),
          )?.latest_revision?.revision,
        ).toBe(2);
      },
      { timeout: 5_000 },
    );
    expect(get).toHaveBeenCalledTimes(6);
  }, 10_000);

  it("refreshes current detail on the first closed response even at the same Revision", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const detail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...projection,
        revision: 1,
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const updatedTurns = projection.turns.slice(0, 1);
    const updatedDetail: CallDetail = {
      ...detail,
      projection: { ...detail.projection!, turns: updatedTurns },
    };
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: base.call_id,
          state: "closed",
          revision: 1,
          turns: updatedTurns,
        }),
        { status: 200, headers: { ETag: '"source-v2"' } },
      ),
    );
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce({
      data: detail,
      response: new Response("{}", { status: 200 }),
    } as never);
    get.mockResolvedValue({
      data: updatedDetail,
      response: new Response("{}", { status: 200 }),
    } as never);
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    await screen.findByRole("table", { name: /reported latency/i });
    await waitFor(
      () => {
        expect(
          within(screen.getByRole("table", { name: /reported latency/i })).getAllByRole(
            "row",
          ),
        ).toHaveLength(updatedTurns.length + 1);
      },
      { timeout: 5_000 },
    );
    expect(get).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(
      client.getQueryData<CallDetail>(
        viewerQueryKey({ projectId: "default", authContextId: "test" }, [
          "call",
          base.call_id,
        ]),
      )?.projection?.turns,
    ).toHaveLength(updatedTurns.length);
  }, 10_000);

  it("refreshes cached detail when the first live response has no cached validator", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const cachedDetail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...projection,
        revision: 1,
        agent: "Cached Agent",
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const refreshedDetail: CallDetail = {
      ...cachedDetail,
      projection: { ...cachedDetail.projection!, agent: "Fresh Agent" },
    };
    client.setQueryData(
      viewerQueryKey({ projectId: "default", authContextId: "test" }, [
        "call",
        base.call_id,
      ]),
      cachedDetail,
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          call_id: base.call_id,
          state: "closed",
          revision: 1,
          turns: cachedDetail.projection?.turns,
        }),
        { status: 200, headers: { ETag: '"first-observed-source-v2"' } },
      ),
    );
    const get = vi.spyOn(api, "GET").mockResolvedValue({
      data: refreshedDetail,
      response: new Response("{}", { status: 200 }),
    } as never);
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Fresh Agent")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);
  }, 10_000);

  it("refreshes a detail read completed before the first closed live response", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const cachedDetail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...projection,
        revision: 1,
        agent: "Cached Agent",
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const refreshedDetail: CallDetail = {
      ...cachedDetail,
      projection: { ...cachedDetail.projection!, agent: "Fresh Agent" },
    };
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce({
      data: cachedDetail,
      response: new Response("{}", { status: 200 }),
    } as never);
    get.mockResolvedValueOnce({
      data: refreshedDetail,
      response: new Response("{}", { status: 200 }),
    } as never);
    let resolveLive!: (response: Response) => void;
    const liveRead = new Promise<Response>((resolve) => {
      resolveLive = resolve;
    });
    const fetcher = vi.spyOn(globalThis, "fetch").mockReturnValue(liveRead as never);
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Cached Agent")).toBeInTheDocument();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await act(async () => {
      resolveLive(
        new Response(
          JSON.stringify({
            call_id: base.call_id,
            state: "closed",
            revision: 1,
            turns: cachedDetail.projection?.turns,
          }),
          { status: 200, headers: { ETag: '"first-observed-source-v2"' } },
        ),
      );
    });

    expect(await screen.findByText("Fresh Agent")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
  }, 10_000);

  it("does not restart a slow detail retry while it is still in flight", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0, staleTime: 0, gcTime: Infinity } },
    });
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const cachedDetail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...projection,
        revision: 1,
        agent: "Cached Agent",
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const refreshedDetail: CallDetail = {
      ...cachedDetail,
      projection: { ...cachedDetail.projection!, agent: "Fresh Agent" },
    };
    client.setQueryData(
      viewerQueryKey({ projectId: "default", authContextId: "test" }, [
        "call",
        base.call_id,
      ]),
      cachedDetail,
    );
    let rejectInitial!: (error: Error) => void;
    let resolveRetry!: (result: { data: CallDetail; response: Response }) => void;
    const initialRead = new Promise<never>((_, reject) => {
      rejectInitial = reject;
    });
    const retryRead = new Promise<{ data: CallDetail; response: Response }>((resolve) => {
      resolveRetry = resolve;
    });
    const get = vi.spyOn(api, "GET");
    get.mockReturnValueOnce(initialRead as never).mockReturnValueOnce(retryRead as never);
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: base.call_id,
          state: "closed",
          revision: 1,
          turns: cachedDetail.projection?.turns,
        }),
        { status: 200, headers: { ETag: '"source-v2"' } },
      ),
    );
    fetcher.mockResolvedValue(
      new Response(null, { status: 304, headers: { ETag: '"source-v2"' } }),
    );
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await act(async () => rejectInitial(new Error("first detail read failed")));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(get).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveRetry({
        data: refreshedDetail,
        response: new Response("{}", { status: 200 }),
      });
      await retryRead;
    });
    expect(await screen.findByText("Fresh Agent")).toBeInTheDocument();
  }, 10_000);

  it.each([401, 403])(
    "hides cached detail and stops polling after HTTP %i",
    async (status) => {
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
        },
      });
      const base = callDetailFixture as CallDetail;
      const projection = base.projection as CallProjection;
      const cachedDetail: CallDetail = {
        ...base,
        state: "closed",
        latest_revision: { ...base.latest_revision!, revision: 1 },
        projection_state: "stale",
        projection: {
          ...projection,
          revision: 1,
          media: projection.media.map((media) => ({ ...media, peaks_path: null })),
        },
      };
      client.setQueryData(
        viewerQueryKey({ projectId: "default", authContextId: "test" }, [
          "call",
          base.call_id,
        ]),
        cachedDetail,
      );
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(
          JSON.stringify({
            call_id: base.call_id,
            state: "closed",
            revision: 2,
            turns: cachedDetail.projection?.turns,
          }),
          { status: 200, headers: { ETag: '"revision-2"' } },
        ),
      );
      const get = vi
        .spyOn(api, "GET")
        .mockRejectedValue(
          new ApiError(status, status === 401 ? "unauthorized" : "forbidden"),
        );
      const { factory } = fakeClocks();

      render(
        <QueryClientProvider client={client}>
          <ViewerQueryScopeProvider
            scope={{ projectId: "default", authContextId: "test" }}
          >
            <MediaClockContext.Provider value={factory}>
              <CallView callId={base.call_id} />
            </MediaClockContext.Provider>
          </ViewerQueryScopeProvider>
        </QueryClientProvider>,
      );

      await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("Call access unavailable")).toBeInTheDocument();
      expect(
        screen.queryByRole("table", { name: /reported latency/i }),
      ).not.toBeInTheDocument();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 2_200));
      });
      expect(get).toHaveBeenCalledTimes(1);
    },
    10_000,
  );

  it.each([401, 403])(
    "hides cached detail when the live route returns HTTP %i",
    async (status) => {
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
        },
      });
      const base = callDetailFixture as CallDetail;
      const detail: CallDetail = {
        ...base,
        state: "closed",
        latest_revision: { ...base.latest_revision!, revision: 1 },
        projection_state: "current",
        projection: {
          ...(base.projection as CallProjection),
          revision: 1,
          media: (base.projection as CallProjection).media.map((media) => ({
            ...media,
            peaks_path: null,
          })),
        },
      };
      client.setQueryData(
        viewerQueryKey({ projectId: "default", authContextId: "test" }, [
          "call",
          base.call_id,
        ]),
        detail,
      );
      const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(JSON.stringify({ error: { code: "FORBIDDEN" } }), {
          status,
        }),
      );
      const get = vi.spyOn(api, "GET");
      const { factory } = fakeClocks();

      render(
        <QueryClientProvider client={client}>
          <ViewerQueryScopeProvider
            scope={{ projectId: "default", authContextId: "test" }}
          >
            <MediaClockContext.Provider value={factory}>
              <CallView callId={base.call_id} />
            </MediaClockContext.Provider>
          </ViewerQueryScopeProvider>
        </QueryClientProvider>,
      );

      expect(await screen.findByText("Call access unavailable")).toBeInTheDocument();
      expect(
        screen.queryByRole("table", { name: /reported latency/i }),
      ).not.toBeInTheDocument();
      expect(get).not.toHaveBeenCalled();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 2_200));
      });
      expect(fetcher).toHaveBeenCalledTimes(1);
    },
    10_000,
  );

  it("hides cached detail when the live route reports a purged Call", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      },
    });
    const base = callDetailFixture as CallDetail;
    const detail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "current",
      projection: {
        ...(base.projection as CallProjection),
        revision: 1,
        media: (base.projection as CallProjection).media.map((media) => ({
          ...media,
          peaks_path: null,
        })),
      },
    };
    client.setQueryData(
      viewerQueryKey({ projectId: "default", authContextId: "test" }, [
        "call",
        base.call_id,
      ]),
      detail,
    );
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "CALL_NOT_FOUND" } }), {
        status: 404,
      }),
    );
    const get = vi.spyOn(api, "GET");
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText("Call not found", {}, { timeout: 5_000 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /reported latency/i })).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();
  }, 10_000);

  it("sends the ETag and keeps cached turns after an unchanged response", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: "call_0123456789abcdef0123456789abcdef",
          state: "open",
          revision: null,
          turns: [{}],
        }),
        { status: 200, headers: { ETag: '"live-turns-v1"' } },
      ),
    );
    fetcher.mockResolvedValue(
      new Response(null, { status: 304, headers: { ETag: '"live-turns-v1"' } }),
    );

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <LiveTurnsProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("open:1")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });

    expect(fetcher).toHaveBeenCalledTimes(2);
    const [, init] = fetcher.mock.calls[1];
    expect(new Headers(init?.headers).get("If-None-Match")).toBe('"live-turns-v1"');
    expect(screen.getByText("open:1")).toBeInTheDocument();
  });

  it("keeps polling a closed Call until its projection is current", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const get = vi.spyOn(api, "GET");
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          call_id: "call_0123456789abcdef0123456789abcdef",
          state: "closed",
          revision: null,
          turns: [],
        }),
        { status: 200, headers: { ETag: '"closed-without-revision"' } },
      ),
    );
    get.mockImplementation(((path: string) => {
      const data =
        path === "/v1/calls/{call_id}"
          ? ({
              call_id: "call_0123456789abcdef0123456789abcdef",
              state: "closed",
              latest_revision: null,
              projection_state: "none",
            } as unknown as CallDetail)
          : undefined;
      return Promise.resolve({
        data,
        response: new Response("{}", { status: 200 }),
      });
    }) as never);

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <ClosedCallProbe />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("closed:closed:none")).toBeInTheDocument();
    expect(get).toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(await screen.findByText("closed:closed:none")).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(2);
  }, 10_000);

  it("switches from the closed live Revision turns to the durable projection", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const fetcher = vi.spyOn(globalThis, "fetch");
    const base = callDetailFixture as CallDetail;
    const projection = base.projection as CallProjection;
    const staleDetail: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: { ...base.latest_revision!, revision: 1 },
      projection_state: "stale",
      projection: {
        ...projection,
        revision: 0,
        turns: [],
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    const durable: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: base.latest_revision,
      projection_state: "current",
      projection: {
        ...projection,
        turns: projection.turns.slice(0, 1),
        media: projection.media.map((media) => ({ ...media, peaks_path: null })),
      },
    };
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          call_id: base.call_id,
          state: "closed",
          revision: 1,
          turns: projection.turns,
        }),
        { status: 200, headers: { ETag: '"closed-revision-1"' } },
      ),
    );
    const get = vi.spyOn(api, "GET");
    get
      .mockResolvedValueOnce({
        data: staleDetail,
        response: new Response("{}", { status: 200 }),
      } as never)
      .mockResolvedValue({
        data: durable,
        response: new Response("{}", { status: 200 }),
      } as never);
    const { factory } = fakeClocks();

    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
          <MediaClockContext.Provider value={factory}>
            <CallView callId={base.call_id} />
          </MediaClockContext.Provider>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      const table = screen.getByRole("table", { name: /reported latency/i });
      expect(within(table).getAllByRole("row")).toHaveLength(
        durable.projection!.turns.length + 1,
      );
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(
      within(screen.getByRole("table", { name: /reported latency/i })).getAllByRole(
        "row",
      ),
    ).toHaveLength(durable.projection!.turns.length + 1);
    expect(
      client.getQueryData<CallDetail>(
        viewerQueryKey({ projectId: "default", authContextId: "test" }, [
          "call",
          base.call_id,
        ]),
      )?.projection_state,
    ).toBe("current");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_200));
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
