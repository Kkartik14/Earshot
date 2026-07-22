export interface ViewerSessionStatus {
  authenticated: boolean;
  authentication_required: boolean;
  project_id: string;
  csrf_token: string | null;
  expires_in_seconds: number | null;
}

interface SessionExchange {
  project_id: string;
  csrf_token: string;
  expires_in_seconds: number;
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

/** Null means this deployment requires a project key and has no valid cookie. */
export async function getViewerSession(): Promise<ViewerSessionStatus | null> {
  const response = await fetch("/v1/auth/session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401) return null;
  return json<ViewerSessionStatus>(response);
}

export async function exchangeProjectKey(
  credential: string,
): Promise<ViewerSessionStatus> {
  const response = await fetch("/v1/auth/session", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { Authorization: `Bearer ${credential}` },
  });
  const issued = await json<SessionExchange>(response);
  return {
    authenticated: true,
    authentication_required: true,
    ...issued,
  };
}

export async function logoutViewerSession(csrfToken: string): Promise<void> {
  const response = await fetch("/v1/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "X-Earshot-CSRF": csrfToken },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}
