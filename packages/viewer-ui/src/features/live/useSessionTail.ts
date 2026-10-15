import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useViewerQueryScope } from "../../api/queryScope";
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

/** Subscribe to a live session's admitted journal facts. */
export function useSessionTail(
  sessionId: string | undefined,
  options?: Pick<TailOptions, "eventSourceFactory" | "staleAfterMs" | "maxErrors">,
): SessionTail {
  const scope = useViewerQueryScope();
  const store = useMemo(
    () => new LiveStore(),
    [scope.projectId, scope.authContextId, sessionId],
  );
  const [attempt, setAttempt] = useState(0);
  const callbackIdentity = useMemo(() => ({ store, attempt }), [store, attempt]);
  const [connectionState, setConnectionState] = useState<{
    key: typeof callbackIdentity | null;
    value: TailConnection;
  }>({ key: null, value: { kind: "connecting" } });
  const connection =
    connectionState.key === callbackIdentity
      ? connectionState.value
      : ({ kind: "connecting" } as const);
  const [lastEventState, setLastEventState] = useState(() => ({
    key: null as LiveStore | null,
    value: Date.now(),
  }));
  const [now, setNow] = useState(() => Date.now());
  const lastEventAt = lastEventState.key === store ? lastEventState.value : now;
  const settings = useRef(options);
  settings.current = options;

  const facts = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );

  useEffect(() => {
    if (sessionId == null) return;
    let active = true;
    store.clear();
    const handle = openSessionTail(sessionId, {
      ...settings.current,
      onEvent: (event) => {
        if (!active) return;
        setLastEventState({ key: store, value: Date.now() });
        store.apply(event);
      },
      onConnection: (value) => {
        if (active) setConnectionState({ key: callbackIdentity, value });
      },
    });
    return () => {
      active = false;
      handle.close();
    };
  }, [sessionId, attempt, callbackIdentity, store]);

  // Keep page activity time separate from session facts.
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
