import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { ObserveFeature } from "./ObserveFeature";
import { ViewerQueryScopeProvider } from "./api/queryScope";

describe("ObserveFeature embedding", () => {
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
});
