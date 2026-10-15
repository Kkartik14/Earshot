import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { ViewerQueryScopeProvider, type ViewerQueryScope } from "../../api/queryScope";
import type { TailOptions } from "../../api/tail";
import { useSessionTail } from "./useSessionTail";
import { vi } from "vitest";

class FakeEventSource {
  closed = false;
  readyState = 1;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string) {}

  addEventListener(name: string, listener: EventListener) {
    const listeners = this.listeners.get(name) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(name, listeners);
  }

  close() {
    this.closed = true;
    this.readyState = 2;
  }

  fail() {
    this.onerror?.(new Event("error"));
  }

  emit(name: string, data: unknown, id: string) {
    const event = new MessageEvent(name, {
      data: JSON.stringify(data),
      lastEventId: id,
    });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

function LiveProbe({
  sessionId,
  sources,
  snapshots,
}: {
  sessionId: string;
  sources: FakeEventSource[];
  snapshots?: {
    sessionId: string;
    factsSessionId: string | null;
    connection: string;
    silentForMs: number;
  }[];
}) {
  const options: Pick<TailOptions, "eventSourceFactory"> = {
    eventSourceFactory: (url) => {
      const source = new FakeEventSource(url);
      sources.push(source);
      return source as unknown as EventSource;
    },
  };
  const { facts, connection, silentForMs, reconnect } = useSessionTail(
    sessionId,
    options,
  );
  useLayoutEffect(() => {
    snapshots?.push({
      sessionId,
      factsSessionId: facts.sessionId,
      connection: connection.kind,
      silentForMs,
    });
  }, [connection.kind, facts.sessionId, sessionId, silentForMs, snapshots]);
  return (
    <>
      <output data-testid="live-session">{facts.sessionId ?? "empty"}</output>
      <output data-testid="silence">{silentForMs}</output>
      <button type="button" onClick={reconnect}>
        Reconnect
      </button>
    </>
  );
}

function scopedView(
  scope: ViewerQueryScope,
  client: QueryClient,
  sources: FakeEventSource[],
  sessionId = "same-session-id",
  snapshots?: {
    sessionId: string;
    factsSessionId: string | null;
    connection: string;
    silentForMs: number;
  }[],
) {
  return (
    <QueryClientProvider client={client}>
      <ViewerQueryScopeProvider scope={scope}>
        <LiveProbe sessionId={sessionId} sources={sources} snapshots={snapshots} />
      </ViewerQueryScopeProvider>
    </QueryClientProvider>
  );
}

describe("useSessionTail auth scope", () => {
  it("closes the previous stream and drops its facts when project scope changes", () => {
    const sources: FakeEventSource[] = [];
    const client = new QueryClient();
    const projectA = { projectId: "project-a", authContextId: "auth-a" };
    const projectB = { projectId: "project-b", authContextId: "auth-b" };
    const view = render(scopedView(projectA, client, sources));

    expect(sources).toHaveLength(1);
    act(() => {
      sources[0].emit("open", { session_id: "project-a-session" }, "journal-a:1");
    });
    expect(screen.getByTestId("live-session")).toHaveTextContent("project-a-session");

    view.rerender(scopedView(projectB, client, sources));

    expect(sources[0].closed).toBe(true);
    expect(sources).toHaveLength(2);
    expect(screen.getByTestId("live-session")).toHaveTextContent("empty");
  });

  it("never renders the previous session's facts or status during a route transition", () => {
    const sources: FakeEventSource[] = [];
    const snapshots: {
      sessionId: string;
      factsSessionId: string | null;
      connection: string;
      silentForMs: number;
    }[] = [];
    const client = new QueryClient();
    const scope = { projectId: "project-a", authContextId: "auth-a" };
    const view = render(scopedView(scope, client, sources, "session-a", snapshots));

    act(() => {
      sources[0].emit("open", { session_id: "session-a" }, "journal-a:1");
    });
    expect(screen.getByTestId("live-session")).toHaveTextContent("session-a");
    snapshots.length = 0;

    view.rerender(scopedView(scope, client, sources, "session-b", snapshots));

    expect(sources[0].closed).toBe(true);
    expect(sources).toHaveLength(2);
    expect(sources[1].url).toContain("session-b");
    expect(screen.getByTestId("live-session")).toHaveTextContent("empty");
    expect(snapshots.length).toBeGreaterThan(0);
    expect(
      snapshots.every(
        (snapshot) =>
          snapshot.sessionId !== "session-b" ||
          (snapshot.factsSessionId == null && snapshot.connection === "connecting"),
      ),
    ).toBe(true);
  });

  it("ignores late callbacks when returning to a previous session route", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000));
    const sources: FakeEventSource[] = [];
    const snapshots: {
      sessionId: string;
      factsSessionId: string | null;
      connection: string;
      silentForMs: number;
    }[] = [];
    const client = new QueryClient();
    const scope = { projectId: "project-a", authContextId: "auth-a" };
    const view = render(scopedView(scope, client, sources, "session-a", snapshots));

    act(() => {
      sources[0].emit("open", { session_id: "session-a" }, "journal-a:1");
      sources[0].fail();
    });
    view.rerender(scopedView(scope, client, sources, "session-b", snapshots));
    expect(sources[0].closed).toBe(true);

    vi.setSystemTime(new Date(5_000));
    act(() => sources[0].emit("heartbeat", {}, ""));
    act(() => vi.advanceTimersByTime(1_000));
    snapshots.length = 0;
    view.rerender(scopedView(scope, client, sources, "session-a", snapshots));

    expect(sources[1].closed).toBe(true);
    expect(sources).toHaveLength(3);
    expect(sources[2].url).toContain("session-a");
    expect(screen.getByTestId("live-session")).toHaveTextContent("empty");
    expect(snapshots.length).toBeGreaterThan(0);
    expect(
      snapshots.every(
        (snapshot) =>
          snapshot.sessionId !== "session-a" ||
          (snapshot.factsSessionId == null &&
            snapshot.connection === "connecting" &&
            snapshot.silentForMs === 0),
      ),
    ).toBe(true);

    view.unmount();
    vi.useRealTimers();
  });

  it("keeps the silence age increasing across reconnect until a new event arrives", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000));
    const sources: FakeEventSource[] = [];
    const client = new QueryClient();
    const scope = { projectId: "project-a", authContextId: "auth-a" };
    const view = render(scopedView(scope, client, sources, "session-a"));

    try {
      act(() => {
        sources[0].emit("open", { session_id: "session-a" }, "journal-a:1");
      });
      act(() => vi.advanceTimersByTime(5_000));
      const silenceBeforeReconnect = Number(screen.getByTestId("silence").textContent);
      expect(silenceBeforeReconnect).toBeGreaterThan(0);

      fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
      expect(sources).toHaveLength(2);
      const silenceAfterReconnect = Number(screen.getByTestId("silence").textContent);
      expect(silenceAfterReconnect).toBeGreaterThanOrEqual(silenceBeforeReconnect);

      act(() => vi.advanceTimersByTime(1_000));
      expect(Number(screen.getByTestId("silence").textContent)).toBeGreaterThan(
        silenceAfterReconnect,
      );
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
