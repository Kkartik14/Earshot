import { describe, expect, it } from "vitest";

import { createTraceContext, injectTraceHeaders } from "./trace-context.js";
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
});
