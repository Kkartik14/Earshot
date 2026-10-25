/** Verify the real OpenTelemetry processor exports spans with the session key needed to resolve a call. */
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { setTimeout as sleep } from "node:timers/promises";
import { beforeAll, describe, expect, inject, it } from "vitest";
import { EarshotClient, EarshotSpanProcessor, withSessionKey } from "../src/index.js";

const { baseUrl, apiKey } = inject("earshot");
const earshot = new EarshotClient({ baseUrl, apiKey });

function tracing(processor: EarshotSpanProcessor) {
  const exporter = new OTLPTraceExporter({
    url: `${baseUrl}/v1/traces`,
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  const provider = new BasicTracerProvider({
    spanProcessors: [processor, new SimpleSpanProcessor(exporter)],
  });
  return { tracer: provider.getTracer("earshot-node-test"), provider };
}

async function spanCount(sessionKey: string): Promise<number> {
  const { call_id } = await earshot.resolveCall(sessionKey);
  return (await earshot.getCall(call_id)).span_count;
}

beforeAll(() => {
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  return () => context.disable();
});

describe("EarshotSpanProcessor", () => {
  it("stamps the session key of withSessionKey on every span started within it", async () => {
    const { tracer, provider } = tracing(new EarshotSpanProcessor());

    withSessionKey("node-span-context", () =>
      tracer.startActiveSpan("turn", (turn) => {
        tracer.startSpan("llm").end();
        turn.end();
      }),
    );
    await withSessionKey("node-span-async", async () => {
      await sleep(5);
      tracer.startSpan("tts").end();
    });
    await provider.forceFlush();

    expect(await spanCount("node-span-context")).toBe(2);
    expect(await spanCount("node-span-async")).toBe(1);
    await provider.shutdown();
  });

  it("stamps a fixed key outside any context, and leaves a span's own key alone", async () => {
    const { tracer, provider } = tracing(
      new EarshotSpanProcessor({ sessionKey: "node-fixed" }),
    );

    tracer.startSpan("stt").end();
    withSessionKey("node-fixed-context", () => tracer.startSpan("llm").end());
    tracer
      .startSpan("tool", { attributes: { "earshot.session.id": "node-fixed-own" } })
      .end();
    await provider.forceFlush();

    expect(await spanCount("node-fixed")).toBe(1);
    expect(await spanCount("node-fixed-context")).toBe(1);
    expect(await spanCount("node-fixed-own")).toBe(1);
    await provider.shutdown();
  });

  it("refuses a key the server would not read: 1 to 256 bytes of UTF-8 after trimming", () => {
    for (const bad of [
      "",
      "   ",
      "x".repeat(257),
      "é".repeat(129),
      "NOT_GIVEN",
      "\ud800",
    ]) {
      expect(() => new EarshotSpanProcessor({ sessionKey: bad })).toThrow(RangeError);
      expect(() => withSessionKey(bad, () => undefined)).toThrow(RangeError);
    }
    expect(withSessionKey("é".repeat(128), () => "ran")).toBe("ran");
  });
});
