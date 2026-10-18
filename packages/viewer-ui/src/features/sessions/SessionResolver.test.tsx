import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useLiveSessions, useSessionIncidents } from "../../api/hooks";
import { SessionResolver } from "./SessionResolver";

const hookMocks = vi.hoisted(() => ({
  useSessionIncidents: vi.fn(),
  useLiveSessions: vi.fn(),
}));

vi.mock("../../api/hooks", () => hookMocks);
vi.mock("../inspector/SessionInspector", () => ({
  SessionInspector: ({ bundleId }: { bundleId: string }) => <h1>artifact:{bundleId}</h1>,
}));
vi.mock("../live/LiveSessionView", () => ({
  LiveSessionView: ({ sessionId }: { sessionId: string }) => <h1>live:{sessionId}</h1>,
}));

const result = (data: unknown, overrides: Record<string, unknown> = {}) =>
  ({ data, isError: false, isPending: false, ...overrides }) as never;

const incident = (bundleId: string) => ({
  bundle_id: bundleId,
  session_id: "session-1",
  status: "completed",
  finality: "final",
  framework: "fixture",
});

const incidentPages = (...items: ReturnType<typeof incident>[]) => ({
  pages: [{ items, next_cursor: null }],
  pageParams: [undefined],
});

function renderResolver(sessionId = "session-1") {
  return render(
    <SessionResolver
      sessionId={sessionId}
      LinkComponent={({ href, children, ...props }) => (
        <a href={href} {...props}>
          {children}
        </a>
      )}
    />,
  );
}

beforeEach(() => {
  vi.mocked(useSessionIncidents).mockReturnValue(
    result(incidentPages()) as ReturnType<typeof useSessionIncidents>,
  );
  vi.mocked(useLiveSessions).mockReturnValue(
    result({ items: [] }) as ReturnType<typeof useLiveSessions>,
  );
});

describe("Platform session resolution", () => {
  it("opens the only retained artifact for the session", () => {
    vi.mocked(useSessionIncidents).mockReturnValue(
      result(incidentPages(incident("bundle-1"))) as ReturnType<
        typeof useSessionIncidents
      >,
    );

    renderResolver();

    expect(useLiveSessions).toHaveBeenCalledWith({ enabled: false });
    expect(
      screen.getByRole("heading", { name: "artifact:bundle-1" }),
    ).toBeInTheDocument();
  });

  it("asks the user to choose when a session has multiple artifacts", () => {
    vi.mocked(useSessionIncidents).mockReturnValue(
      result(incidentPages(incident("bundle-1"), incident("bundle-2"))) as ReturnType<
        typeof useSessionIncidents
      >,
    );

    renderResolver();

    expect(
      screen.getByRole("heading", { name: /more than one artifact/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /bundle-2/i })).toHaveAttribute(
      "href",
      "/sessions/bundle-2",
    );
  });

  it("does not open the first result until all artifacts have been checked", () => {
    const fetchNextPage = vi.fn();
    vi.mocked(useSessionIncidents).mockReturnValue(
      result(
        {
          pages: [{ items: [incident("bundle-1")], next_cursor: "next" }],
          pageParams: [undefined],
        },
        { hasNextPage: true, fetchNextPage },
      ) as ReturnType<typeof useSessionIncidents>,
    );

    renderResolver();

    expect(
      screen.getByRole("heading", { name: /more than one artifact/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /bundle-1/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more artifacts" }));
    expect(fetchNextPage).toHaveBeenCalledOnce();
    expect(screen.queryByRole("heading", { name: "artifact:bundle-1" })).toBeNull();
  });

  it("keeps found artifacts visible and offers retry after a later page fails", () => {
    const fetchNextPage = vi.fn();
    vi.mocked(useSessionIncidents).mockReturnValue(
      result(
        {
          pages: [{ items: [incident("bundle-1")], next_cursor: "next" }],
          pageParams: [undefined],
        },
        {
          isError: true,
          isFetchNextPageError: true,
          hasNextPage: true,
          fetchNextPage,
        },
      ) as ReturnType<typeof useSessionIncidents>,
    );

    renderResolver();

    expect(screen.getByRole("link", { name: /bundle-1/i })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /artifacts already found remain available/i,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry loading more artifacts" }));
    expect(fetchNextPage).toHaveBeenCalledOnce();
  });

  it("shows an in-progress session when there is no stored artifact", () => {
    vi.mocked(useLiveSessions).mockReturnValue(
      result({ items: [{ session_id: "session-1" }] }) as ReturnType<
        typeof useLiveSessions
      >,
    );

    renderResolver();

    expect(screen.getByRole("heading", { name: "live:session-1" })).toBeInTheDocument();
  });

  it("does not describe missing evidence as a failed or empty call", () => {
    renderResolver();

    expect(
      screen.getByText(/this does not show whether the call completed or failed/i),
    ).toBeInTheDocument();
  });

  it("surfaces lookup failures instead of rendering an absent state", () => {
    vi.mocked(useSessionIncidents).mockReturnValue(
      result(undefined, { isError: true }) as ReturnType<typeof useSessionIncidents>,
    );

    renderResolver();

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/this does not show whether/i)).not.toBeInTheDocument();
  });
});
