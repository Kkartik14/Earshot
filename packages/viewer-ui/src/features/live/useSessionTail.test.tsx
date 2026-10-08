import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { ViewerQueryScopeProvider, type ViewerQueryScope } from "../../api/queryScope";
import type { TailOptions } from "../../api/tail";
import { useSessionTail } from "./useSessionTail";

class FakeEventSource {
  closed = false;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string) {}

  addEventListener(name: string, listener: EventListener) {
    const listeners = this.listeners.get(name) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(name, listeners);
  }

  close() {
    this.closed = true;
  }

  emit(name: string, data: unknown, id: string) {
    const event = new MessageEvent(name, {
      data: JSON.stringify(data),
      lastEventId: id,
    });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

function LiveProbe({ sources }: { sources: FakeEventSource[] }) {
  const options: Pick<TailOptions, "eventSourceFactory"> = {
    eventSourceFactory: (url) => {
      const source = new FakeEventSource(url);
      sources.push(source);
      return source as unknown as EventSource;
    },
  };
  const { facts } = useSessionTail("same-session-id", options);
  return <output data-testid="live-session">{facts.sessionId ?? "empty"}</output>;
}

function scopedView(
  scope: ViewerQueryScope,
  client: QueryClient,
  sources: FakeEventSource[],
) {
  return (
    <QueryClientProvider client={client}>
      <ViewerQueryScopeProvider scope={scope}>
        <LiveProbe sources={sources} />
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
});
