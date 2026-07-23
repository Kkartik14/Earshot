import { describe, expect, it } from "vitest";

import {
  createTraceContext,
  injectTraceHeaders,
  parseTraceParent,
} from "./trace-context.js";
import type { RandomSource } from "./types.js";
import { sequentialRandom } from "./testing/fakes.js";

const allZeroRandom: RandomSource = (bytes) => bytes.fill(0);

describe("createTraceContext", () => {
  it("mints a spec-valid, sampled W3C traceparent", () => {
    const ctx = createTraceContext(sequentialRandom(1));

    expect(ctx.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    expect(ctx.traceId).toHaveLength(32);
    expect(ctx.spanId).toHaveLength(16);
    expect(ctx.traceparent).toBe(`00-${ctx.traceId}-${ctx.spanId}-01`);
  });

  it("never emits the invalid all-zero trace/span id", () => {
    const ctx = createTraceContext(allZeroRandom);
    expect(ctx.traceId).not.toMatch(/^0+$/);
    expect(ctx.spanId).not.toMatch(/^0+$/);
  });

  it("is deterministic under a fixed random source", () => {
    expect(createTraceContext(sequentialRandom(5)).traceparent).toBe(
      createTraceContext(sequentialRandom(5)).traceparent,
    );
  });
});

describe("injectTraceHeaders", () => {
  it("sets traceparent and merges without mutating the input", () => {
    const ctx = createTraceContext(sequentialRandom(1));
    const base = { authorization: "Bearer x" };

    const headers = injectTraceHeaders(ctx, base);

    expect(headers.traceparent).toBe(ctx.traceparent);
    expect(headers.authorization).toBe("Bearer x");
    expect(base).not.toHaveProperty("traceparent"); // input untouched
  });

  it("preserves a traceparent already present on the headers (never clobbers the app's)", () => {
    const ctx = createTraceContext(sequentialRandom(1));
    const app = "00-1234567890abcdef1234567890abcdef-1122334455667788-01";

    const headers = injectTraceHeaders(ctx, { traceparent: app });

    expect(headers.traceparent).toBe(app);
  });
});

describe("parseTraceParent", () => {
  it("parses a spec-valid traceparent into its trace/span ids", () => {
    const app = "00-1234567890abcdef1234567890abcdef-1122334455667788-01";
    const ctx = parseTraceParent(app);
    expect(ctx).not.toBeNull();
    expect(ctx!.traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(ctx!.spanId).toBe("1122334455667788");
    expect(ctx!.traceparent).toBe(app);
  });

  it("returns null for absent, malformed, or all-zero ids", () => {
    expect(parseTraceParent(undefined)).toBeNull();
    expect(parseTraceParent("")).toBeNull();
    expect(parseTraceParent("not-a-traceparent")).toBeNull();
    // all-zero trace id is invalid per the spec
    expect(
      parseTraceParent("00-00000000000000000000000000000000-1122334455667788-01"),
    ).toBeNull();
    // all-zero span id is invalid per the spec
    expect(
      parseTraceParent("00-1234567890abcdef1234567890abcdef-0000000000000000-01"),
    ).toBeNull();
  });
});
