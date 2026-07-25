import { QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { notifyViewerSessionInvalid } from "./api/client";
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
  useLiveSessions: () => ({
    isPending: false,
    isError: false,
    isSuccess: true,
    data: { items: [], limitations: [], following_journal_directory: false },
  }),
}));

describe("App", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows a reachability error when session discovery cannot contact the API", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));

    renderWithProviders(<App />);

    expect(
      await screen.findByText("Unable to reach the Earshot API."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Project API key")).toBeInTheDocument();
  });

  it("renders the shell and the fleet dashboard when loopback auth is optional", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          authenticated: false,
          authentication_required: false,
          project_id: "default",
          csrf_token: null,
          expires_in_seconds: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    renderWithProviders(<App />);
    expect(await screen.findByText("earshot")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Fleet metrics" }),
    ).toBeInTheDocument();
  });

  it("exchanges an entered API key without writing it to browser storage", async () => {
    const credential = "earshot_sk_test.secret-value";
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const fetch = vi.spyOn(globalThis, "fetch");
    fetch
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project_id: "default",
            csrf_token: "csrf-token",
            expires_in_seconds: 3600,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      );

    renderWithProviders(<App />);
    const input = await screen.findByLabelText("Project API key");
    fireEvent.change(input, { target: { value: credential } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("heading", { name: "Fleet metrics" });
    const exchange = fetch.mock.calls[1];
    const headers = new Headers((exchange[1] as RequestInit).headers);
    expect(headers.get("Authorization")).toBe(`Bearer ${credential}`);
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(credential);
    expect(screen.queryByLabelText("Project API key")).not.toBeInTheDocument();
  });

  it("returns once to login and clears cached project data when the session expires", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          authenticated: true,
          authentication_required: true,
          project_id: "default",
          csrf_token: "csrf-from-session",
          expires_in_seconds: 3600,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const clear = vi.spyOn(QueryClient.prototype, "clear");
    renderWithProviders(<App />);
    await screen.findByRole("heading", { name: "Fleet metrics" });

    act(() => {
      notifyViewerSessionInvalid();
      notifyViewerSessionInvalid();
    });

    expect(await screen.findByLabelText("Project API key")).toBeInTheDocument();
    expect(
      screen.getByText("Your viewer session expired. Sign in again."),
    ).toBeInTheDocument();
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it("logs out with the in-memory CSRF token", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    fetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            authenticated: true,
            authentication_required: true,
            project_id: "default",
            csrf_token: "csrf-from-session",
            expires_in_seconds: 3600,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    renderWithProviders(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await screen.findByLabelText("Project API key");

    const logout = fetch.mock.calls[1];
    const headers = new Headers((logout[1] as RequestInit).headers);
    expect(headers.get("X-Earshot-CSRF")).toBe("csrf-from-session");
  });

  it("does not claim logout succeeded when the server rejects it", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            authenticated: true,
            authentication_required: true,
            project_id: "default",
            csrf_token: "csrf-from-session",
            expires_in_seconds: 3600,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(new Response(null, { status: 503 }));

    renderWithProviders(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(
      await screen.findByText("Sign out failed. Your viewer session is still active."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Project API key")).not.toBeInTheDocument();
  });
});
