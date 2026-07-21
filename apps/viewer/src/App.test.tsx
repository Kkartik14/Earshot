import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { renderWithProviders } from "./test/utils";

vi.mock("./api/hooks", () => ({
  useIncidents: () => ({
    isPending: false,
    isError: false,
    isSuccess: true,
    data: { items: [], next_cursor: null },
  }),
  useTurnMetrics: () => ({
    isPending: false,
    isError: false,
    isSuccess: true,
    data: { metric: "first_token_ms", group_by: "model", groups: [] },
  }),
}));

describe("App", () => {
  it("renders the shell and the fleet dashboard at the index route", () => {
    renderWithProviders(<App />);
    expect(screen.getByText("earshot")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fleet metrics" })).toBeInTheDocument();
  });
});
