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
}));

describe("App", () => {
  it("renders the shell and the empty state at the index route", () => {
    renderWithProviders(<App />);
    expect(screen.getByText("earshot")).toBeInTheDocument();
    expect(screen.getByText("Select a session")).toBeInTheDocument();
  });
});
