import { describe, expect, it } from "vitest";
import { ApiError, unwrap } from "./client";

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
});
