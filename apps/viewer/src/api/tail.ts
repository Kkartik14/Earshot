import { api, unwrap } from "./client";

/** The event names the backend's `/v1/live/.../tail` stream emits. */
export const TAIL_EVENT_NAMES = [
  "open",
  "record",
  /** A record exists at this slot and its content may not leave for this
   *  destination. Subscribed to deliberately: an unsubscribed event name is one
   *  the page never hears about, which would turn a governed stream back into a
   *  silently shortened one. */
  "withheld",
  "operation_open",
  "limit",
  "exhausted",
  "finalize",
  "replay_truncated",
  "reset",
  "overflow",
  "end",
  "heartbeat",
] as const;

export type TailEventName = (typeof TAIL_EVENT_NAMES)[number];

export interface TailEvent {
  name: TailEventName;
  /** The journal slot this event occupies, or null for a control event. */
  sequence: number | null;
  journalId: string | null;
  data: Record<string, unknown>;
}

/** How the transport itself is doing — never a statement about the session. */
export type TailConnection =
  | { kind: "connecting" }
  | { kind: "open" }
  /** No event, not even a heartbeat, for longer than the stale window. */
  | { kind: "stale"; silentForMs: number }
  | { kind: "closed"; reason: "server_closed" | "connection_failed" | "client_closed" };

export interface TailHandle {
  close(): void;
}

export interface TailOptions {
  onEvent(event: TailEvent): void;
  onConnection(state: TailConnection): void;
  /** Silence — including heartbeats — after which the connection reads as stale. */
  staleAfterMs?: number;
  /** Consecutive transport errors tolerated before the tail gives up. */
  maxErrors?: number;
  /** Injectable for tests; defaults to the platform `EventSource`. */
  eventSourceFactory?: (url: string) => EventSource;
}

/** Split an SSE `id` of the form `<journal_id>:<sequence>`. */
export function parseEventId(id: string | null | undefined): {
  journalId: string | null;
  sequence: number | null;
} {
  if (!id) return { journalId: null, sequence: null };
  const separator = id.lastIndexOf(":");
  if (separator <= 0) return { journalId: null, sequence: null };
  const sequence = Number(id.slice(separator + 1));
  if (!Number.isInteger(sequence) || sequence < 1) {
    return { journalId: id.slice(0, separator), sequence: null };
  }
  return { journalId: id.slice(0, separator), sequence };
}

/** Events that occupy a journal slot. Everything else is a control event whose
 *  `lastEventId` is only the previously delivered one, because the server sends
 *  control events without an `id` so they cannot advance a resume cursor. */
const SEQUENCED: ReadonlySet<string> = new Set([
  "open",
  "record",
  "withheld",
  "operation_open",
  "limit",
  "exhausted",
  "finalize",
]);

/** Subscribe to one live session.
 *
 *  The transport is `EventSource`, and therefore an ordinary same-origin `GET`:
 *  it carries the viewer's HttpOnly session cookie, it is subject to the
 *  same-origin policy, and it resumes with `Last-Event-ID` without this code
 *  tracking a cursor. Nothing here interprets the facts — that is `liveStore`'s
 *  job — and nothing here computes one. */
export function openSessionTail(sessionId: string, options: TailOptions): TailHandle {
  const staleAfterMs = options.staleAfterMs ?? 30_000;
  const maxErrors = options.maxErrors ?? 5;
  const url = `/v1/live/sessions/${encodeURIComponent(sessionId)}/tail`;
  const source = (options.eventSourceFactory ?? ((target) => new EventSource(target)))(
    url,
  );

  let closed = false;
  let errors = 0;
  let lastEventAt = Date.now();
  let staleReported = false;
  let watchdog = 0;

  options.onConnection({ kind: "connecting" });

  const shutdown = (reason: "server_closed" | "connection_failed" | "client_closed") => {
    if (closed) return;
    closed = true;
    window.clearInterval(watchdog);
    source.close();
    options.onConnection({ kind: "closed", reason });
  };

  const dispatch = (name: TailEventName) => (message: MessageEvent<string>) => {
    lastEventAt = Date.now();
    if (staleReported) {
      staleReported = false;
      options.onConnection({ kind: "open" });
    }
    errors = 0;
    let data: Record<string, unknown> = {};
    try {
      const parsed: unknown = JSON.parse(message.data);
      if (parsed != null && typeof parsed === "object") {
        data = parsed as Record<string, unknown>;
      }
    } catch {
      // A frame we cannot parse is dropped rather than guessed at. It is a
      // protocol fault, and inventing a shape for it would be worse.
      return;
    }
    const { journalId, sequence } = parseEventId(message.lastEventId);
    options.onEvent({
      name,
      sequence: SEQUENCED.has(name) ? sequence : null,
      journalId,
      data,
    });
    // The server closes the stream after these; do not let the browser
    // silently reconnect underneath a client that has been told the stream ended.
    if (name === "end") shutdown("server_closed");
  };

  for (const name of TAIL_EVENT_NAMES) {
    source.addEventListener(name, dispatch(name) as EventListener);
  }

  source.addEventListener("open", () => {
    // The transport-level `open` (not the journal header event of the same name).
    errors = 0;
    lastEventAt = Date.now();
    options.onConnection({ kind: "open" });
  });

  source.onerror = () => {
    if (closed) return;
    errors += 1;
    if (errors < maxErrors && source.readyState !== 2 /* CLOSED */) {
      // The browser is retrying with Last-Event-ID; a gap is recoverable.
      options.onConnection({ kind: "stale", silentForMs: Date.now() - lastEventAt });
      staleReported = true;
      return;
    }
    shutdown("connection_failed");
    // EventSource never exposes a status code, so "the viewer session expired"
    // and "the backend is unreachable" are indistinguishable here. One probe
    // against the ordinary API settles it: `unwrap` raises the existing
    // session-invalid signal on a 401, and anything else really is unreachable.
    void unwrap(api.GET("/v1/live/sessions")).catch(() => undefined);
  };

  watchdog = window.setInterval(() => {
    if (closed || staleReported) return;
    const silentForMs = Date.now() - lastEventAt;
    if (silentForMs >= staleAfterMs) {
      staleReported = true;
      options.onConnection({ kind: "stale", silentForMs });
    }
  }, 1_000);

  return {
    close() {
      shutdown("client_closed");
    },
  };
}
