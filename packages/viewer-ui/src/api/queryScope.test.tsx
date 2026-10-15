import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { useIncident, useProjectSummary } from "./hooks";
import { api } from "./client";
import {
  clearViewerQueries,
  ViewerQueryScopeProvider,
  viewerQueryKey,
  type ViewerQueryScope,
} from "./queryScope";

afterEach(() => {
  vi.restoreAllMocks();
});

function ScopeProbe({ bundleId }: { bundleId: string }) {
  const summary = useProjectSummary();
  const incident = useIncident(bundleId);
  return (
    <div>
      <output data-testid="summary">{summary.data?.project_id ?? summary.status}</output>
      <output data-testid="detail">
        {incident.data?.profile.session.session_id ?? incident.status}
      </output>
    </div>
  );
}

function ScopedProbe({ scope, bundleId }: { scope: ViewerQueryScope; bundleId: string }) {
  return (
    <ViewerQueryScopeProvider scope={scope}>
      <ScopeProbe bundleId={bundleId} />
    </ViewerQueryScopeProvider>
  );
}

describe("viewer query scope", () => {
  it("does not render cached project A detail or summary after switching to project B", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: Infinity } },
    });
    const projectA = { projectId: "project-a", authContextId: "auth-a" };
    const projectB = { projectId: "project-b", authContextId: "auth-b" };
    client.setQueryData(
      viewerQueryKey(projectA, ["project-summary"]),
      { project_id: "project-a", items: [] },
      { updatedAt: 0 },
    );
    client.setQueryData(
      viewerQueryKey(projectA, ["incident", "bundle-1"]),
      { profile: { session: { session_id: "project-a-session" } } },
      { updatedAt: 0 },
    );

    const requestSignals: AbortSignal[] = [];
    vi.spyOn(api, "GET").mockImplementation(((
      _path: string,
      options?: { signal?: AbortSignal },
    ) => {
      const signal = options?.signal;
      if (signal == null) throw new Error("project queries must pass their abort signal");
      requestSignals.push(signal);
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("request aborted", "AbortError")),
          { once: true },
        );
      }) as never;
    }) as typeof api.GET);

    const view = render(
      <QueryClientProvider client={client}>
        <ScopedProbe scope={projectA} bundleId="bundle-1" />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText("project-a", { selector: "output" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("project-a-session", { selector: "output" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(requestSignals).toHaveLength(2));

    act(() =>
      view.rerender(
        <QueryClientProvider client={client}>
          <ScopedProbe scope={projectB} bundleId="bundle-1" />
        </QueryClientProvider>,
      ),
    );

    expect(
      screen.queryByText("project-a", { selector: "output" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("project-a-session", { selector: "output" }),
    ).not.toBeInTheDocument();
    expect(requestSignals.slice(0, 2).every((signal) => signal.aborted)).toBe(true);
    expect(
      client.getQueryData(viewerQueryKey(projectA, ["project-summary"])),
    ).toBeUndefined();
    expect(
      client.getQueryData(viewerQueryKey(projectA, ["incident", "bundle-1"])),
    ).toBeUndefined();
  });

  it("clears Earshot cache entries without removing host-owned queries", () => {
    const client = new QueryClient();
    client.setQueryData(["platform", "projects"], ["project-a"]);
    client.setQueryData(["earshot", "project-a", "auth-a", "incident", "bundle-1"], {
      profile: { session: { session_id: "project-a-session" } },
    });

    clearViewerQueries(client);

    expect(client.getQueryData(["platform", "projects"])).toEqual(["project-a"]);
    expect(client.getQueryCache().findAll({ queryKey: ["earshot"] })).toHaveLength(0);
  });
});
