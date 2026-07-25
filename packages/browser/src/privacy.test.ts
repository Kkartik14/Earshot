import { describe, expect, it } from "vitest";

import { makeSalt, opaqueDeviceId } from "./privacy.js";
import { sequentialRandom } from "./testing/fakes.js";

describe("opaqueDeviceId", () => {
  it("produces a stable, prefixed opaque id for the same salt + input", () => {
    const a = opaqueDeviceId("Jabra Elite 75t", "salt123");
    const b = opaqueDeviceId("Jabra Elite 75t", "salt123");
    expect(a).toBe(b);
    expect(a).toMatch(/^dev_[0-9a-f]{8}$/);
  });

  it("never contains the raw device string", () => {
    const hash = opaqueDeviceId("Jabra Elite 75t", "salt123");
    expect(hash).not.toContain("Jabra");
    expect(hash).not.toContain("Elite");
  });

  it("is unlinkable across sessions (different salt -> different id)", () => {
    const one = opaqueDeviceId("device-abc", "session-1-salt");
    const two = opaqueDeviceId("device-abc", "session-2-salt");
    expect(one).not.toBe(two);
  });

  it("supports a custom prefix and omits empty input", () => {
    expect(opaqueDeviceId("sink-1", "salt", "sink")).toMatch(/^sink_[0-9a-f]{8}$/);
    expect(opaqueDeviceId("", "salt")).toBeUndefined();
    expect(opaqueDeviceId(undefined, "salt")).toBeUndefined();
  });
});

describe("makeSalt", () => {
  it("returns fixed-width hex for the requested byte length", () => {
    expect(makeSalt(sequentialRandom(1), 8)).toHaveLength(16);
    expect(makeSalt(sequentialRandom(1), 4)).toHaveLength(8);
    expect(makeSalt(sequentialRandom(1), 8)).toMatch(/^[0-9a-f]+$/);
  });
});
