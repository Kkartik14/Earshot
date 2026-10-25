import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ObserveRoutes } from "./App";
import { renderWithProviders } from "./test/utils";

// Which view each URL mounts. The views' data and behaviour are tested in
// @earshot/viewer-ui against a real server; here only the route table is checked.
describe("viewer routes", () => {
  it("serves the calls list at /calls", () => {
    renderWithProviders(<ObserveRoutes />, { route: "/calls" });

    expect(screen.getByRole("heading", { level: 1, name: "Calls" })).toBeInTheDocument();
    expect(screen.getByRole("search", { name: "Filter calls" })).toBeInTheDocument();
  });

  it("serves one call at /calls/:callId", () => {
    renderWithProviders(<ObserveRoutes />, {
      route: "/calls/call_0d0d68c6f99a393adc2596ec43815ea9",
    });

    expect(screen.getByRole("status")).toHaveTextContent("Loading call…");
    expect(screen.queryByRole("search", { name: "Filter calls" })).toBeNull();
  });

  it("serves the dashboard at /dashboard, linked from the rail", () => {
    renderWithProviders(<ObserveRoutes />, { route: "/dashboard" });

    expect(
      screen.getByRole("heading", { level: 1, name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "href",
      "/dashboard",
    );
  });

  it("serves the upload form at /upload, linked from the rail", () => {
    renderWithProviders(<ObserveRoutes />, { route: "/upload" });

    expect(
      screen.getByRole("heading", { level: 1, name: "Upload a call" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Upload a call" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upload a call" })).toHaveAttribute(
      "href",
      "/upload",
    );
  });

  it("links the calls list from the rail on every page", () => {
    renderWithProviders(<ObserveRoutes />, { route: "/" });

    expect(screen.getByRole("link", { name: "Calls" })).toHaveAttribute("href", "/calls");
  });
});
