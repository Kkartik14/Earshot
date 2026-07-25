import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { openSessionTail, type TailConnection, type TailOptions } from "../../api/tail";
import { LiveStore, type LiveFacts } from "./liveStore";

export interface SessionTail {
  facts: LiveFacts;
  connection: TailConnection;
  /** Milliseconds since the last event of any kind, heartbeats included. */
  silentForMs: number;
  /** Reopen after the tail gave up. Always an explicit user action. */
  reconnect(): void;
}

/** Subscribe one component to one live session.
 *
 *  The returned facts are only ever what the stream said. Nothing here derives,
 *  averages, or extrapolates: a live session has no digest, so anything computed
 *  from it would be a claim no artifact attests. */
export function useSessionTail(
  sessionId: string | undefined,
  options?: Pick<TailOptions, "eventSourceFactory" | "staleAfterMs" | "maxErrors">,
): SessionTail {
  const store = useMemo(() => new LiveStore(), []);
  const [connection, setConnection] = useState<TailConnection>({ kind: "connecting" });
  const [attempt, setAttempt] = useState(0);
  const [lastEventAt, setLastEventAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const settings = useRef(options);
  settings.current = options;

  const facts = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );

  useEffect(() => {
    if (sessionId == null) return;
    store.clear();
    const handle = openSessionTail(sessionId, {
      ...settings.current,
      onEvent: (event) => {
        setLastEventAt(Date.now());
        store.apply(event);
      },
      onConnection: setConnection,
    });
    return () => handle.close();
  }, [sessionId, attempt, store]);

  // A ticking "seconds ago" is a fact about this page, not about the session, so
  // it is computed here and never folded into the facts themselves.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  return {
    facts,
    connection,
    silentForMs: Math.max(0, now - lastEventAt),
    reconnect: () => setAttempt((value) => value + 1),
  };
}
