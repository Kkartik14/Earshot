import createClient from "openapi-fetch";
import type { paths } from "./schema";

/** Typed client for the Earshot backend. Same-origin: proxied in dev, served by
 *  FastAPI in production. */
export const api = createClient<paths>({ baseUrl: "/" });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type SessionInvalidListener = () => void;
const sessionInvalidListeners = new Set<SessionInvalidListener>();

/** Protected API 401s invalidate the in-memory viewer session. */
export function onViewerSessionInvalid(listener: SessionInvalidListener): () => void {
  sessionInvalidListeners.add(listener);
  return () => sessionInvalidListeners.delete(listener);
}

export function notifyViewerSessionInvalid(): void {
  for (const listener of sessionInvalidListeners) listener();
}

/** Auth failures are deterministic and must never enter a React Query retry storm. */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return false;
  }
  return failureCount < 3;
}

type OpenApiResult<T> = { data?: T; error?: unknown; response: Response };

/** Collapse openapi-fetch's `{ data, error, response }` into a value-or-throw
 *  contract, surfacing the backend's stable error code. Lets React Query treat
 *  failures as exceptions. */
export async function unwrap<T>(result: Promise<OpenApiResult<T>>): Promise<T> {
  const { data, error, response } = await result;
  if (!response.ok || data === undefined) {
    const code =
      (error as { error?: { code?: string } } | undefined)?.error?.code ??
      `HTTP ${response.status}`;
    const apiError = new ApiError(response.status, code);
    if (response.status === 401) notifyViewerSessionInvalid();
    throw apiError;
  }
  return data;
}
