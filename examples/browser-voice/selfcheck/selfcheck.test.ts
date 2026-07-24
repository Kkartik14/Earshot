/**
 * Installation self-check, as a release gate.
 *
 * `pnpm run selfcheck` builds `@earshot/browser` and then runs this file with
 * Vitest, which exits non-zero on any failed assertion. It imports the BUILT
 * package (via the workspace `exports`) and drives it against the SDK's own test
 * fakes to prove `drain()` yields a server-shaped `CapturePayload`.
 *
 * The substance lives in `assertConsumable()` so the same check can gate a
 * release from any runner, not only Vitest.
 */

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
