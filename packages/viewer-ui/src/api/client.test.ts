import { afterEach, describe, expect, it } from "vitest";
import {
  ApiError,
  apiUrl,
  configureApiBasePath,
  onViewerSessionInvalid,
  shouldRetryQuery,
  unwrap,
} from "./client";

afterEach(() => configureApiBasePath(""));

function result<T>(init: { data?: T; error?: unknown; ok: boolean; status: number }) {
  return Promise.resolve({
    data: init.data,
    error: init.error,
    response: { ok: init.ok, status: init.status } as Response,
  });
}

describe("unwrap", () => {
  it("returns data on success", async () => {
    await expect(
      unwrap(result({ data: { a: 1 }, ok: true, status: 200 })),
    ).resolves.toEqual({
      a: 1,
    });
  });

  it("throws ApiError carrying the backend error code", async () => {
    await expect(
      unwrap(
        result({
          error: { error: { code: "EARSHOT_INCIDENT_NOT_FOUND" } },
          ok: false,
          status: 404,
        }),
      ),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "EARSHOT_INCIDENT_NOT_FOUND",
    });
    expect(new ApiError(500, "x")).toBeInstanceOf(Error);
  });

  it("invalidates a viewer session on 401 but preserves a 403 session", async () => {
    let invalidations = 0;
    const unsubscribe = onViewerSessionInvalid(() => invalidations++);
    await expect(unwrap(result({ ok: false, status: 401 }))).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(unwrap(result({ ok: false, status: 403 }))).rejects.toBeInstanceOf(
      ApiError,
    );
    unsubscribe();

    expect(invalidations).toBe(1);
  });

  it("never retries authorization failures", () => {
    expect(shouldRetryQuery(0, new ApiError(401, "expired"))).toBe(false);
    expect(shouldRetryQuery(0, new ApiError(403, "forbidden"))).toBe(false);
    expect(shouldRetryQuery(0, new ApiError(503, "unavailable"))).toBe(true);
    expect(shouldRetryQuery(3, new ApiError(503, "unavailable"))).toBe(false);
  });
});

describe("same-origin API base path", () => {
  it("prefixes API and session paths", () => {
    configureApiBasePath("/platform/earshot/");

    expect(apiUrl("/v1/auth/session")).toBe("/platform/earshot/v1/auth/session");
  });

  it.each(["https://example.test/api", "//example.test/api", "/api/../other"])(
    "rejects non-local or traversing paths: %s",
    (path) => {
      expect(() => configureApiBasePath(path)).toThrow(TypeError);
    },
  );
});
