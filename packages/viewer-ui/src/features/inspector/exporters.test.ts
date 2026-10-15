import { describe, expect, it } from "vitest";
import { EXPORT_FORMATS, exportRefusal } from "./exporters";

describe("EXPORT_FORMATS", () => {
  it("mirrors the spec's exporter enum", () => {
    expect([...EXPORT_FORMATS].sort()).toEqual(["openinference", "otlp"]);
  });
});

describe("exportRefusal", () => {
  it("reports a non-API failure as an unanswered backend, never an empty document", () => {
    const refusal = exportRefusal(new Error("boom"));
    expect(refusal.code).toBe("unavailable");
    expect(refusal.detail).toMatch(/did not answer/i);
  });
});
