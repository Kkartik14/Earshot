/** Release check that the built browser package emits a server-consumable payload. */

import { describe, expect, it } from "vitest";

import { assertConsumable } from "./consumable.js";

describe("installation self-check: @earshot/browser is consumable from the workspace", () => {
  it("drains a CapturePayload of the shape the server engines expect", async () => {
    const payload = await assertConsumable();
    expect(payload.captureVersion).toBe(2);
    expect(payload.snapshots.length).toBeGreaterThan(0);
    expect(payload.deviceEvents.length).toBeGreaterThan(0);
    expect(payload.traceContext.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  });
});
