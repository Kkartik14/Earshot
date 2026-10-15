import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ObserveFeature } from "./ObserveFeature";
import { ViewerQueryScopeProvider } from "./api/queryScope";

const mocks = vi.hoisted(() => ({ useTurnMetrics: vi.fn() }));

vi.mock("./api/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/hooks")>();
  return { ...actual, useTurnMetrics: mocks.useTurnMetrics };
});

describe("ObserveFeature embedding", () => {
  beforeEach(() => {
    mocks.useTurnMetrics.mockReset();
  });

  it("fits the host shell and omits Earshot's standalone session rail", () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider
          scope={{ projectId: "project-a", authContextId: "auth-a" }}
        >
          <ObserveFeature
            route={{
              kind: "call-status",
              callId: "call-1",
              capture: { state: "waiting" },
            }}
            embedded
          />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    const root = screen
      .getByRole("heading", { name: "call-1" })
      .closest("[data-earshot-viewer]");
    expect(root).not.toBeNull();
    if (root == null) throw new Error("embedded Observe root was not rendered");
    expect(root).toHaveAttribute("data-embedded", "true");
    expect(root.querySelector('[data-embedded="true"]')).not.toBeNull();
    expect(root.querySelector("main")).toBeNull();
    expect(root.querySelector("aside")).toBeNull();
  });

  it("does not tell an embedded host to ingest when its fleet has no turns", () => {
    mocks.useTurnMetrics.mockReturnValue({
      isPending: false,
      isError: false,
      isSuccess: true,
      data: { groups: [] },
    });
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider
          scope={{ projectId: "project-a", authContextId: "auth-a" }}
        >
          <ObserveFeature route={{ kind: "fleet" }} embedded />
        </ViewerQueryScopeProvider>
      </QueryClientProvider>,
    );

    expect(
      screen.getByText("No turn metrics are available for this project yet."),
    ).toBeInTheDocument();
  });
});
