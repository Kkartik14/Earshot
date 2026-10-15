"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  exchangeProjectKey,
  clearViewerQueries,
  getViewerSession,
  logoutViewerSession,
  onViewerSessionInvalid,
  ViewerQueryScopeProvider,
  type ViewerSessionStatus,
} from "@earshot/viewer-ui";
import styles from "./StandaloneAuth.module.css";

type AuthState =
  | { kind: "loading" }
  | { kind: "login"; error?: string }
  | { kind: "ready"; session: ViewerSessionStatus };

function Login({
  initialError,
  onAuthenticated,
}: {
  initialError?: string;
  onAuthenticated: (session: ViewerSessionStatus) => void;
}) {
  const [credential, setCredential] = useState("");
  const [error, setError] = useState(initialError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const supplied = credential;
    setSubmitting(true);
    setError(undefined);
    try {
      onAuthenticated(await exchangeProjectKey(supplied));
    } catch (cause) {
      setError(
        cause instanceof TypeError
          ? "Unable to reach the Earshot API."
          : "The project API key was not accepted.",
      );
    } finally {
      setCredential("");
      setSubmitting(false);
    }
  };

  return (
    <main className={styles.loginPage}>
      <form className={styles.loginCard} onSubmit={submit}>
        <span className={styles.brand}>earshot</span>
        <h1>Open the voice observability viewer</h1>
        <p>
          Enter a project API key once. Earshot exchanges it for an expiring, HttpOnly
          browser session and does not save the key in browser storage.
        </p>
        <label>
          <span>Project API key</span>
          <input
            type="password"
            value={credential}
            onChange={(event) => setCredential(event.target.value)}
            autoComplete="current-password"
            spellCheck={false}
            required
            autoFocus
          />
        </label>
        {error ? (
          <div className={styles.error} role="alert">
            {error}
          </div>
        ) : null}
        <button type="submit" disabled={submitting || credential.length === 0}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

export function StandaloneAuth({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [auth, setAuth] = useState<AuthState>({ kind: "loading" });
  const [logoutError, setLogoutError] = useState<string>();
  const [signingOut, setSigningOut] = useState(false);
  const expiryHandled = useRef(false);

  useEffect(
    () =>
      onViewerSessionInvalid(() => {
        if (expiryHandled.current) return;
        expiryHandled.current = true;
        clearViewerQueries(queryClient);
        setAuth({ kind: "login", error: "Your viewer session expired. Sign in again." });
      }),
    [queryClient],
  );

  useEffect(() => {
    let active = true;
    getViewerSession()
      .then((session) => {
        if (active)
          setAuth(session == null ? { kind: "login" } : { kind: "ready", session });
      })
      .catch(() => {
        if (active) setAuth({ kind: "login", error: "Unable to reach the Earshot API." });
      });
    return () => {
      active = false;
    };
  }, []);

  if (auth.kind === "loading") {
    return <main className={styles.loginPage}>Checking viewer session…</main>;
  }
  if (auth.kind === "login") {
    return (
      <Login
        initialError={auth.error}
        onAuthenticated={(session) => {
          expiryHandled.current = false;
          setAuth({ kind: "ready", session });
        }}
      />
    );
  }

  const signOut = async () => {
    const csrf = auth.session.csrf_token;
    if (csrf == null) return;
    setSigningOut(true);
    setLogoutError(undefined);
    try {
      await logoutViewerSession(csrf);
      clearViewerQueries(queryClient);
      setAuth({ kind: "login" });
    } catch {
      setLogoutError("Sign out failed. Your viewer session is still active.");
    } finally {
      setSigningOut(false);
    }
  };

  return (
    <>
      {auth.session.authenticated ? (
        <div className={styles.signOutControl}>
          {logoutError ? <span role="alert">{logoutError}</span> : null}
          <button
            type="button"
            className={styles.signOut}
            onClick={signOut}
            disabled={signingOut}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      ) : null}
      <ViewerQueryScopeProvider
        scope={{
          projectId: auth.session.project_id,
          authContextId: auth.session.auth_context_id,
        }}
      >
        {children}
      </ViewerQueryScopeProvider>
    </>
  );
}
