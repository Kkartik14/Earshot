import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onViewerSessionInvalid } from "./client";
import {
  openSessionTail,
  parseEventId,
  type TailConnection,
  type TailEvent,
} from "./tail";

// The tail's only non-stream call is the auth probe it makes after giving up.
// Intercepting it at the client seam keeps the test about the tail's behaviour
// rather than about openapi-fetch's transport.
const { probe } = vi.hoisted(() => ({ probe: vi.fn() }));
vi.mock("./client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./client")>();
  return { ...actual, api: { GET: probe } };
});

/** A stand-in for the browser's EventSource that a test can drive by hand. */
class FakeEventSource {
  static readonly instances: FakeEventSource[] = [];
  readyState = 0;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListener): void {
    const set = this.listeners.get(name) ?? new Set<EventListener>();
    set.add(listener);
    this.listeners.set(name, set);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  connect(): void {
    this.readyState = 1;
    for (const listener of this.listeners.get("open") ?? []) listener(new Event("open"));
  }

  emit(name: string, data: unknown, id?: string): void {
    const message = new MessageEvent(name, {
      data: typeof data === "string" ? data : JSON.stringify(data),
      lastEventId: id ?? "",
    });
    for (const listener of this.listeners.get(name) ?? []) listener(message);
  }

  fail({ terminal }: { terminal: boolean } = { terminal: false }): void {
    this.readyState = terminal ? 2 : 0;
    this.onerror?.(new Event("error"));
  }
}

function subscribe() {
  const events: TailEvent[] = [];
  const connections: TailConnection[] = [];
  const handle = openSessionTail("s-1", {
    onEvent: (event) => events.push(event),
    onConnection: (state) => connections.push(state),
    eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
  });
  const source = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  return { events, connections, handle, source };
}

beforeEach(() => {
  FakeEventSource.instances.length = 0;
  probe.mockReset();
  probe.mockResolvedValue({
    data: undefined,
    error: { error: { code: "EARSHOT_UNAUTHORIZED" } },
    response: { ok: false, status: 401 },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("parseEventId", () => {
  it("splits a journal id from its sequence and refuses anything else", () => {
    expect(parseEventId("abc:7")).toEqual({ journalId: "abc", sequence: 7 });
    expect(parseEventId(null)).toEqual({ journalId: null, sequence: null });
    expect(parseEventId("")).toEqual({ journalId: null, sequence: null });
    expect(parseEventId("abc")).toEqual({ journalId: null, sequence: null });
    expect(parseEventId("abc:0")).toEqual({ journalId: "abc", sequence: null });
  });
});

describe("openSessionTail", () => {
  it("subscribes same-origin so the viewer's HttpOnly cookie carries the auth", () => {
    const { source, handle } = subscribe();
    expect(source.url).toBe("/v1/live/sessions/s-1/tail");
    handle.close();
  });

  it("delivers journal-slot events with their sequence", () => {
    const { events, source, handle } = subscribe();
    source.connect();
    source.emit("open", { journal_id: "j", session_id: "s-1" }, "j:1");
    source.emit("record", { kind: "event", value: {} }, "j:2");
    expect(events.map((event) => [event.name, event.sequence])).toEqual([
      ["open", 1],
      ["record", 2],
    ]);
    handle.close();
  });

  it("never reads a sequence off a control event", () => {
    const { events, source, handle } = subscribe();
    source.connect();
    source.emit("record", { kind: "event" }, "j:4");
    // The server sends control events with no id, so the browser reports the
    // previously delivered one. Treating that as a position would silently
    // re-apply an already-seen record.
    source.emit("replay_truncated", { withheld_records: 2 }, "j:4");
    expect(events[1].sequence).toBeNull();
    handle.close();
  });

  it("drops an unparseable frame instead of guessing at its shape", () => {
    const { events, source, handle } = subscribe();
    source.connect();
    source.emit("record", "{not json", "j:2");
    expect(events).toHaveLength(0);
    handle.close();
  });

  it("stops the browser reconnecting after the server said the stream ended", () => {
    const { connections, source, handle } = subscribe();
    source.connect();
    source.emit("end", { reason: "journal_removed", close_observed: false });
    expect(source.closed).toBe(true);
    expect(connections.at(-1)).toEqual({ kind: "closed", reason: "server_closed" });
    handle.close();
  });

  it("reports a recoverable transport error as stale, not as closed", () => {
    const { connections, source, handle } = subscribe();
    source.connect();
    source.fail();
    expect(connections.at(-1)?.kind).toBe("stale");
    expect(source.closed).toBe(false);
    handle.close();
  });

  it("gives up after repeated failures and reports an expired viewer session", async () => {
    let invalidated = false;
    const unsubscribe = onViewerSessionInvalid(() => {
      invalidated = true;
    });
    const { connections, source, handle } = subscribe();
    source.connect();
    for (let attempt = 0; attempt < 5; attempt += 1) source.fail();
    expect(connections.at(-1)).toEqual({ kind: "closed", reason: "connection_failed" });
    expect(source.closed).toBe(true);
    // EventSource never exposes a status code, so one ordinary API call is what
    // distinguishes an expired viewer session from an unreachable backend.
    expect(probe).toHaveBeenCalledWith("/v1/live/sessions");
    await vi.waitFor(() => expect(invalidated).toBe(true));
    unsubscribe();
    handle.close();
  });

  it("reports silence — heartbeats included — as stale", () => {
    vi.useFakeTimers();
    const { connections, source, handle } = subscribe();
    source.connect();
    vi.advanceTimersByTime(31_000);
    expect(connections.at(-1)?.kind).toBe("stale");

    source.emit("heartbeat", { as_of_sequence: 3, close_observed: false });
    expect(connections.at(-1)).toEqual({ kind: "open" });
    handle.close();
  });

  it("closes on request without reporting a server-side ending", () => {
    const { connections, source, handle } = subscribe();
    source.connect();
    handle.close();
    expect(source.closed).toBe(true);
    expect(connections.at(-1)).toEqual({ kind: "closed", reason: "client_closed" });
  });
});
