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
    throw new ApiError(response.status, code);
  }
  return data;
}
