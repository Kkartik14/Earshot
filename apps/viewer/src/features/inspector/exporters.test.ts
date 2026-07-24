import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { EXPORT_FORMATS, exportRefusal } from "./exporters";

describe("EXPORT_FORMATS", () => {
  it("mirrors the spec's exporter enum", () => {
    expect([...EXPORT_FORMATS].sort()).toEqual(["openinference", "otlp"]);
  });
});

describe("exportRefusal", () => {
  it("renders a policy denial as an explicit, coded refusal", () => {
    const refusal = exportRefusal(new ApiError(403, "EARSHOT_EXPORT_DENIED"));
    expect(refusal.code).toBe("EARSHOT_EXPORT_DENIED");
    expect(refusal.title).toMatch(/denied by policy/i);
    expect(refusal.detail).toMatch(/no document was produced/i);
  });

  it("renders an unregistered exporter name as an explicit refusal", () => {
    const refusal = exportRefusal(new ApiError(400, "EARSHOT_UNKNOWN_EXPORT_FORMAT"));
    expect(refusal.code).toBe("EARSHOT_UNKNOWN_EXPORT_FORMAT");
    expect(refusal.title).toMatch(/unknown exporter/i);
  });

  it("reports a non-API failure as an unanswered backend, never an empty document", () => {
    const refusal = exportRefusal(new Error("boom"));
    expect(refusal.code).toBe("unavailable");
    expect(refusal.detail).toMatch(/did not answer/i);
  });
});
