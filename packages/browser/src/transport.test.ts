/**
 * The capture transport's job is not "POST some JSON" — it is to make delivery
 * failure *visible*. These tests pin the four properties the server contract
 * depends on: the payload is versioned and authenticated, retries are bounded
 * and only attempted where they could work, the queue cannot grow without
 * limit, and nothing is ever dropped without coverage saying so.
 *
 * The credential is checked with a sentinel that must appear in exactly one
 * place (the Authorization header) and nowhere else a caller can observe.
 */

import { describe, expect, it, vi } from "vitest";

import { CAPTURE_PROTOCOL_VERSION } from "./protocol.js";
import { EarshotBrowserRecorder } from "./recorder.js";
import { createCaptureTransport } from "./transport.js";
import type { CaptureCoverage, CapturePayload } from "./types.js";
import { FakeFetch, FakeSleep, sequentialRandom } from "./testing/fakes.js";

const ENDPOINT = "https://collector.example/v1/capture";
const API_KEY = "SENTINEL-project-key-do-not-leak";

function payload(overrides: Partial<CapturePayload> = {}): CapturePayload {
  return {
    captureVersion: CAPTURE_PROTOCOL_VERSION,
    sessionId: "sess_abc",
    traceContext: {
      traceparent: `00-${"a".repeat(32)}-${"b".repeat(16)}-01`,
      traceId: "a".repeat(32),
      spanId: "b".repeat(16),
    },
    clockDomain: {
      id: "clk_abc",
      kind: "browser_monotonic",
      unit: "ms",
      uncertaintyMs: 1,
      wallOriginMs: 1_700_000_000_000,
    },
    snapshots: [{ timestamp_ms: 1000, stats: {} }],
    deviceEvents: [{ type: "underrun", timestamp_ms: 1100 }],
    coverage: [],
    ...overrides,
  };
}

/**
 * A `fetch` that holds every call open until `open()` is called, so a test can
 * make the queue genuinely back up behind an in-flight delivery.
 */
function gatedFetch() {
  let opened = false;
  const pending: Array<() => void> = [];
  return {
    open(): void {
      opened = true;
      for (const settle of pending.splice(0)) settle();
    },
    fetch: () =>
      new Promise<{ ok: boolean; status: number }>((resolve) => {
        const settle = (): void => resolve({ ok: true, status: 201 });
        if (opened) settle();
        else pending.push(settle);
      }),
  };
}

/** A minimal coverage sink so a test can assert on what was ledgered. */
function coverageSink() {
  const notes: CaptureCoverage[] = [];
  return {
    notes,
    recordCoverage(note: CaptureCoverage): void {
      notes.push(note);
    },
  };
}

describe("configuration is explicit, never implicit", () => {
  it("requires an endpoint — there is no default collector", () => {
    expect(() =>
      createCaptureTransport({ endpoint: "", fetch: new FakeFetch().fetch }),
    ).toThrow(TypeError);
    expect(() =>
      createCaptureTransport({ endpoint: undefined as unknown as string }),
    ).toThrow(TypeError);
  });

  it("rejects nonsensical bounds instead of silently normalising them", () => {
    const fetcher = new FakeFetch();
    for (const options of [
      { maxAttempts: 0 },
      { maxQueuedPayloads: -1 },
      { retryBackoffMs: Number.NaN },
      { maxRetryBackoffMs: Number.POSITIVE_INFINITY },
    ]) {
      expect(() =>
        createCaptureTransport({ endpoint: ENDPOINT, fetch: fetcher.fetch, ...options }),
      ).toThrow(RangeError);
    }
  });
});

describe("versioned, authenticated delivery", () => {
  it("POSTs the versioned payload with the supplied credential and trace", async () => {
    const fetcher = new FakeFetch([201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      apiKey: API_KEY,
      csrfToken: "csrf-token",
      projectId: "browser-app",
      fetch: fetcher.fetch,
    });

    const result = await transport.send(payload());

    expect(result).toEqual({ delivered: true, attempts: 1, status: 201 });
    const request = fetcher.requests[0]!;
    expect(request.url).toBe(ENDPOINT);
    expect(request.init.method).toBe("POST");
    expect(request.init.headers["content-type"]).toBe("application/json");
    expect(request.init.headers.authorization).toBe(`Bearer ${API_KEY}`);
    expect(request.init.headers["x-earshot-csrf"]).toBe("csrf-token");
    expect(request.init.headers["x-earshot-project-id"]).toBe("browser-app");
    expect(request.init.headers.traceparent).toBe(payload().traceContext.traceparent);
    // Same-origin by default, so a viewer session cookie is actually sent.
    expect(request.init.credentials).toBe("same-origin");
    expect(fetcher.bodies()[0]!.captureVersion).toBe(CAPTURE_PROTOCOL_VERSION);
  });

  it("sends no Authorization header when no key is supplied (cookie auth)", async () => {
    const fetcher = new FakeFetch([201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      csrfToken: "csrf-token",
      fetch: fetcher.fetch,
    });
    await transport.send(payload());
    expect(fetcher.requests[0]!.init.headers.authorization).toBeUndefined();
  });
});

describe("the credential never leaves the Authorization header", () => {
  it("keeps the key out of failures, results and the console", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const warns = vi.spyOn(console, "warn").mockImplementation(() => {});
    const logs = vi.spyOn(console, "log").mockImplementation(() => {});
    const failures: unknown[] = [];
    // "throw" makes the fake reject with a message containing the whole request,
    // which is exactly what a real fetch rejection can do.
    const fetcher = new FakeFetch(["throw"]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      apiKey: API_KEY,
      fetch: fetcher.fetch,
      maxAttempts: 2,
      sleep: new FakeSleep().sleep,
      onFailure: (failure) => failures.push(failure),
    });

    const result = await transport.send(payload());

    expect(result.delivered).toBe(false);
    expect(JSON.stringify(result)).not.toContain(API_KEY);
    expect(JSON.stringify(failures)).not.toContain(API_KEY);
    for (const spy of [errors, warns, logs]) {
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    }
    // The header is the one place it appears.
    expect(fetcher.requests[0]!.init.headers.authorization).toContain(API_KEY);
  });
});

describe("bounded retry and honest failure classification", () => {
  it("retries a transport failure with doubling backoff, then gives up", async () => {
    const sleeps = new FakeSleep();
    const fetcher = new FakeFetch(["throw"]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 3,
      retryBackoffMs: 100,
      sleep: sleeps.sleep,
    });

    const result = await transport.send(payload());

    expect(result.delivered).toBe(false);
    expect(result.attempts).toBe(3);
    expect(fetcher.calls).toBe(3);
    expect(sleeps.delays).toEqual([100, 200]);
    expect(result.failure).toMatchObject({
      kind: "transport",
      retryable: true,
      attempts: 3,
    });
  });

  it("caps the backoff instead of growing it without limit", async () => {
    const sleeps = new FakeSleep();
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([503]).fetch,
      maxAttempts: 5,
      retryBackoffMs: 1000,
      maxRetryBackoffMs: 2500,
      sleep: sleeps.sleep,
    });
    await transport.send(payload());
    expect(sleeps.delays).toEqual([1000, 2000, 2500, 2500]);
  });

  it("stops retrying as soon as the server accepts the batch", async () => {
    const fetcher = new FakeFetch([503, 201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 4,
      sleep: new FakeSleep().sleep,
    });
    const result = await transport.send(payload());
    expect(result).toEqual({ delivered: true, attempts: 2, status: 201 });
    expect(fetcher.calls).toBe(2);
  });

  it.each([
    ["an unsupported capture version", 400],
    ["an unauthenticated caller", 401],
    ["a missing CSRF token", 403],
    ["an oversized batch", 413],
    ["a payload the contract refuses", 422],
  ])("does not retry %s — the answer would not change", async (_case, status) => {
    const fetcher = new FakeFetch([status]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 5,
      sleep: new FakeSleep().sleep,
    });
    const result = await transport.send(payload());
    expect(fetcher.calls).toBe(1);
    expect(result.attempts).toBe(1);
    expect(result.status).toBe(status);
    expect(result.failure).toMatchObject({ kind: "http", retryable: false, status });
  });

  it.each([408, 429, 500, 502, 503, 504])("retries %i", async (status) => {
    const fetcher = new FakeFetch([status]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 2,
      sleep: new FakeSleep().sleep,
    });
    await transport.send(payload());
    expect(fetcher.calls).toBe(2);
  });
});

describe("a drop is never silent", () => {
  it("records the lost observations as coverage when delivery is abandoned", async () => {
    const sink = coverageSink();
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([500]).fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
      coverage: sink,
    });

    const result = await transport.send(payload());

    expect(result.delivered).toBe(false);
    expect(result.failure?.droppedObservations).toBe(2); // 1 snapshot + 1 event
    expect(sink.notes).toContainEqual({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: 2,
    });
  });

  it("forwards the dropped payload's own coverage so those gaps survive", async () => {
    const sink = coverageSink();
    const carried: CaptureCoverage = {
      signal: "webrtc.getstats",
      availability: "partial",
      reason: "getstats_failed",
      droppedCount: 4,
    };
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([500]).fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
      coverage: sink,
    });

    await transport.send(payload({ coverage: [carried] }));

    expect(sink.notes).toContainEqual(carried);
  });

  it("bounds the queue by dropping the OLDEST payload, and says so", async () => {
    const sink = coverageSink();
    // The first POST stays in flight until the gate opens, so the queue backs up.
    const gate = gatedFetch();
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: gate.fetch,
      maxQueuedPayloads: 1,
      coverage: sink,
    });

    const first = transport.send(payload({ sessionId: "sess_1" }));
    const second = transport.send(payload({ sessionId: "sess_2" }));
    const third = transport.send(payload({ sessionId: "sess_3" }));
    expect(transport.queuedCount).toBe(1);

    gate.open();
    const evicted = await second;
    expect(evicted.delivered).toBe(false);
    expect(evicted.failure).toMatchObject({
      kind: "queue_overflow",
      retryable: false,
      sessionId: "sess_2",
    });
    expect(sink.notes).toContainEqual({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_queue_overflow_oldest_dropped",
      droppedCount: 2,
    });

    await transport.flush();
    expect((await first).delivered).toBe(true);
    expect((await third).delivered).toBe(true);
  });

  it("gives up on everything still queued when stopped, with coverage", async () => {
    const sink = coverageSink();
    const gate = gatedFetch();
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: gate.fetch,
      coverage: sink,
    });
    const first = transport.send(payload());
    const queued = transport.send(payload({ sessionId: "sess_queued" }));

    transport.stop();
    expect((await queued).failure).toMatchObject({ sessionId: "sess_queued" });
    expect(sink.notes.some((note) => note.signal === "capture.upload")).toBe(true);

    // A send after stop() is refused rather than buffered forever.
    const refused = await transport.send(payload());
    expect(refused.delivered).toBe(false);

    gate.open();
    await transport.flush();
    expect((await first).delivered).toBe(true);
  });
});

describe("delivery ordering", () => {
  it("posts one payload at a time, in order", async () => {
    const fetcher = new FakeFetch([201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
    });

    const results = await Promise.all([
      transport.send(payload({ sessionId: "sess_1" })),
      transport.send(payload({ sessionId: "sess_2" })),
      transport.send(payload({ sessionId: "sess_3" })),
    ]);

    expect(results.every((result) => result.delivered)).toBe(true);
    expect(fetcher.bodies().map((body) => body.sessionId)).toEqual([
      "sess_1",
      "sess_2",
      "sess_3",
    ]);
  });
});

describe("recorder integration", () => {
  it("an undelivered batch's loss shows up in the NEXT drain's coverage", async () => {
    const recorder = new EarshotBrowserRecorder({ random: sequentialRandom() });
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([500]).fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
      coverage: recorder,
    });

    recorder.recordRenderGlitch("underrun");
    const lost = recorder.drain();
    expect(lost.deviceEvents).toHaveLength(1);
    expect(lost.coverage).toEqual([]);

    await transport.send(lost);

    const next = recorder.drain();
    expect(next.captureVersion).toBe(CAPTURE_PROTOCOL_VERSION);
    expect(next.coverage).toContainEqual({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: 1,
    });
  });
});
