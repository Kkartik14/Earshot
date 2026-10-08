/**
 * The capture transport's job is not "POST some JSON" — it is to make delivery
 * failure *visible*. These tests pin the four properties the server contract
 * depends on: the payload is versioned and authenticated, retries are bounded
 * and only attempted where they could work, the queue cannot grow without
 * limit, and nothing is ever dropped without coverage saying so.
 *
 * Cookie/BFF authentication is checked without exposing a project key to browser
 * code, and CSRF material must stay out of results, failures and logs.
 */

import { describe, expect, it, vi } from "vitest";

import { CAPTURE_PROTOCOL_VERSION } from "./protocol.js";
import { EarshotBrowserRecorder } from "./recorder.js";
import { createCaptureTransport as createTransport } from "./transport.js";
import type { CaptureTransportOptions } from "./transport.js";
import type { CaptureCoverage, CapturePayload } from "./types.js";
import { FakeFetch, FakeSleep, sequentialRandom } from "./testing/fakes.js";

const ENDPOINT = "https://collector.example/v1/capture";
const CSRF_TOKEN = "SENTINEL-csrf-token-do-not-leak";

type TestTransportOptions = Omit<
  CaptureTransportOptions,
  "projectId" | "getAuthContextId"
> & {
  projectId?: string;
  getAuthContextId?: () => string;
};

function createCaptureTransport(options: TestTransportOptions) {
  const { projectId, getAuthContextId, ...rest } = options;
  return createTransport({
    ...rest,
    projectId: projectId ?? "project_test",
    getAuthContextId: getAuthContextId ?? (() => "authctx_test"),
  });
}

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
  const bodies: Array<Record<string, unknown>> = [];
  return {
    bodies,
    open(): void {
      opened = true;
      for (const settle of pending.splice(0)) settle();
    },
    fetch: (_url: string, init: { body: string }) => {
      bodies.push(JSON.parse(init.body) as Record<string, unknown>);
      return new Promise<{ ok: boolean; status: number }>((resolve) => {
        const settle = (): void => resolve({ ok: true, status: 201 });
        if (opened) settle();
        else pending.push(settle);
      });
    },
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
      { maxRecoverySessions: 0.5 },
      { retryBackoffMs: Number.NaN },
      { maxRetryBackoffMs: Number.POSITIVE_INFINITY },
    ]) {
      expect(() =>
        createCaptureTransport({ endpoint: ENDPOINT, fetch: fetcher.fetch, ...options }),
      ).toThrow(RangeError);
    }
  });

  it("requires safe project and opaque auth-context assertions", () => {
    const fetch = new FakeFetch().fetch;
    expect(() =>
      createTransport({
        endpoint: ENDPOINT,
        projectId: "",
        getAuthContextId: () => "authctx_test",
        fetch,
      }),
    ).toThrow(TypeError);
    expect(() =>
      createTransport({
        endpoint: ENDPOINT,
        projectId: "project_test",
        getAuthContextId: () => "\n",
        fetch,
      }),
    ).toThrow(TypeError);
  });
});

describe("versioned, cookie-authenticated delivery", () => {
  it("POSTs through the host BFF with CSRF, project assertion and trace", async () => {
    const fetcher = new FakeFetch([201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      csrfToken: CSRF_TOKEN,
      projectId: "browser-app",
      fetch: fetcher.fetch,
    });

    const result = await transport.send(payload());

    expect(result).toEqual({ delivered: true, attempts: 1, status: 201 });
    const request = fetcher.requests[0]!;
    expect(request.url).toBe(ENDPOINT);
    expect(request.init.method).toBe("POST");
    expect(request.init.headers["content-type"]).toBe("application/json");
    expect(request.init.headers.authorization).toBeUndefined();
    expect(request.init.headers["x-earshot-csrf"]).toBe(CSRF_TOKEN);
    expect(request.init.headers["x-earshot-project-id"]).toBe("browser-app");
    expect(request.init.headers["x-earshot-auth-context-id"]).toBe("authctx_test");
    expect(request.init.headers.traceparent).toBe(payload().traceContext.traceparent);
    // Same-origin by default, so a viewer session cookie is actually sent.
    expect(request.init.credentials).toBe("same-origin");
    expect(fetcher.bodies()[0]!.captureVersion).toBe(CAPTURE_PROTOCOL_VERSION);
  });

  it("retains an exact retry inside its original project and auth context", async () => {
    const fetcher = new FakeFetch([503, 202]);
    let authContextId = "authctx_project_a";
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      projectId: "project-a",
      getAuthContextId: () => authContextId,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });

    const original = payload({
      captureVersion: 2,
      sessionId: "sess_project_a",
      drainSequence: 1,
    });
    const first = await transport.send(original);
    expect(first.failure?.retryRetained).toBe(true);

    authContextId = "authctx_project_b";
    await transport.flush();
    const otherProjectSend = await transport.send(
      payload({
        captureVersion: 2,
        sessionId: "sess_project_b",
        drainSequence: 1,
      }),
    );

    expect(fetcher.calls).toBe(1);
    expect(otherProjectSend.failure).toMatchObject({
      kind: "blocked",
      authContextChanged: true,
      retryable: false,
    });

    authContextId = "authctx_project_a";
    await transport.flush();

    expect(fetcher.calls).toBe(2);
    expect(fetcher.requests[1]!.init.body).toBe(fetcher.requests[0]!.init.body);
    expect(fetcher.requests[0]!.init.headers["x-earshot-project-id"]).toBe("project-a");
    expect(fetcher.requests[1]!.init.headers["x-earshot-project-id"]).toBe("project-a");
    expect(fetcher.requests[0]!.init.headers["x-earshot-auth-context-id"]).toBe(
      "authctx_project_a",
    );
    expect(fetcher.requests[1]!.init.headers["x-earshot-auth-context-id"]).toBe(
      "authctx_project_a",
    );
  });

  it("does not publish an acknowledgement after its authorization context changes in flight", async () => {
    let authContextId = "authctx_project_a";
    let calls = 0;
    const accepted: unknown[] = [];
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      projectId: "project-a",
      getAuthContextId: () => authContextId,
      maxAttempts: 1,
      fetch: async () => {
        calls += 1;
        if (calls === 1) authContextId = "authctx_project_b";
        return {
          ok: true,
          status: 202,
          json: async () => ({ call_id: "call_a", finalized: true }),
        };
      },
      onAccepted: (acknowledgement) => {
        accepted.push(acknowledgement);
      },
    });

    const first = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_project_a", drainSequence: 1 }),
    );

    expect(first).toMatchObject({
      delivered: false,
      failure: { kind: "blocked", authContextChanged: true, retryRetained: true },
    });
    expect(accepted).toEqual([]);

    authContextId = "authctx_project_a";
    const flushResult = await transport.flush();

    expect(accepted).toEqual([
      {
        projectId: "project-a",
        authContextId: "authctx_project_a",
        sessionId: "sess_project_a",
        captureVersion: 2,
        drainSequence: 1,
        callId: "call_a",
        finalized: true,
      },
    ]);
    expect(flushResult).toMatchObject({
      pendingExactRetryCount: 0,
      retryOutcomes: [
        {
          sessionId: "sess_project_a",
          drainSequence: 1,
          delivery: { delivered: true, status: 202 },
        },
      ],
    });
  });

  it("reports an exact retry that remains pending after its bounded flush attempt", async () => {
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      maxAttempts: 1,
      fetch: new FakeFetch([503, 503]).fetch,
    });

    const initial = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_pending", drainSequence: 4 }),
    );
    expect(initial.failure?.retryRetained).toBe(true);

    const flushResult = await transport.flush();

    expect(flushResult).toMatchObject({
      pendingExactRetryCount: 1,
      retryOutcomes: [
        {
          sessionId: "sess_pending",
          drainSequence: 4,
          delivery: {
            delivered: false,
            failure: { retryRetained: true, status: 503 },
          },
        },
      ],
    });
  });
});

describe("authentication material never leaves the request headers", () => {
  it("keeps the CSRF token out of failures, results and the console", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const warns = vi.spyOn(console, "warn").mockImplementation(() => {});
    const logs = vi.spyOn(console, "log").mockImplementation(() => {});
    const failures: unknown[] = [];
    // "throw" makes the fake reject with a message containing the whole request,
    // which is exactly what a real fetch rejection can do.
    const fetcher = new FakeFetch(["throw"]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      csrfToken: CSRF_TOKEN,
      fetch: fetcher.fetch,
      maxAttempts: 2,
      sleep: new FakeSleep().sleep,
      onFailure: (failure) => failures.push(failure),
    });

    const result = await transport.send(payload());

    expect(result.delivered).toBe(false);
    expect(JSON.stringify(result)).not.toContain(CSRF_TOKEN);
    expect(JSON.stringify(failures)).not.toContain(CSRF_TOKEN);
    for (const spy of [errors, warns, logs]) {
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    }
    expect(fetcher.requests[0]!.init.headers["x-earshot-csrf"]).toBe(CSRF_TOKEN);
    expect(fetcher.requests[0]!.init.headers.authorization).toBeUndefined();
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
  it("declares an abandoned v2 drain on the next drain for the same call", async () => {
    const fetcher = new FakeFetch([413, 201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });
    const first = payload({ captureVersion: 2, drainSequence: 1 });
    const second = payload({ captureVersion: 2, drainSequence: 2 });

    expect((await transport.send(first)).delivered).toBe(false);
    expect((await transport.send(second)).delivered).toBe(true);

    expect(fetcher.bodies()[0]!.resync).toBeUndefined();
    expect(fetcher.bodies()[1]!.resync).toEqual({
      missedFromSequence: 1,
      missedThroughSequence: 1,
      reason: "upload_failed_payload_dropped",
    });
    // Recovery metadata belongs to the transport's wire copy, not the caller's payload.
    expect(second.resync).toBeUndefined();
  });

  it("extends the declared range when the recovery drain is also abandoned", async () => {
    const fetcher = new FakeFetch([413, 413, 201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });

    await transport.send(payload({ captureVersion: 2, drainSequence: 1 }));
    await transport.send(payload({ captureVersion: 2, drainSequence: 2 }));
    await transport.send(payload({ captureVersion: 2, drainSequence: 3 }));

    expect(fetcher.bodies()[1]!.resync).toEqual({
      missedFromSequence: 1,
      missedThroughSequence: 1,
      reason: "upload_failed_payload_dropped",
    });
    expect(fetcher.bodies()[2]!.resync).toEqual({
      missedFromSequence: 1,
      missedThroughSequence: 2,
      reason: "upload_failed_payload_dropped",
    });
  });

  it("accepts a smaller terminal replacement at the same unapplied drain sequence", async () => {
    const fetcher = new FakeFetch([202, 413, 202]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });
    const first = payload({
      captureVersion: 2,
      sessionId: "sess_terminal_413",
      drainSequence: 1,
    });
    const terminal = payload({
      captureVersion: 2,
      sessionId: "sess_terminal_413",
      drainSequence: 2,
      end: { reason: "call_ended", timestampMs: 1200 },
    });
    const smallerTerminal = {
      ...terminal,
      snapshots: [],
      deviceEvents: [],
      coverage: [
        {
          signal: "capture.upload",
          availability: "partial",
          reason: "upload_failed_payload_dropped",
          droppedCount: 2,
        },
      ],
    };

    expect((await transport.send(first)).delivered).toBe(true);
    const rejected = await transport.send(terminal);
    expect(rejected).toMatchObject({ delivered: false, status: 413 });
    expect((await transport.send(smallerTerminal)).delivered).toBe(true);

    const [firstBody, rejectedBody, replacementBody] = fetcher.bodies();
    expect(rejectedBody).toMatchObject({
      sessionId: "sess_terminal_413",
      drainSequence: 2,
    });
    expect(replacementBody).toMatchObject({
      sessionId: "sess_terminal_413",
      drainSequence: 2,
      end: terminal.end,
      snapshots: [],
      deviceEvents: [],
      coverage: smallerTerminal.coverage,
    });
    expect(replacementBody?.resync).toBeUndefined();
    expect(firstBody?.drainSequence).toBe(1);
    expect(terminal.snapshots).toHaveLength(1);
  });

  it("resends a blocked terminal drain with the unresolved predecessor gap", async () => {
    const fetcher = new FakeFetch([503, 503, 422, 202, 202]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });
    const predecessor = payload({
      captureVersion: 2,
      sessionId: "sess_terminal_retry",
      drainSequence: 8,
    });
    const terminal = payload({
      captureVersion: 2,
      sessionId: "sess_terminal_retry",
      drainSequence: 9,
      end: { reason: "call_ended", timestampMs: 1200 },
    });

    const predecessorResult = await transport.send(predecessor);
    expect(predecessorResult.failure).toMatchObject({
      kind: "http",
      retryRetained: true,
    });
    const blockedTerminal = await transport.send(terminal);
    expect(blockedTerminal.failure?.kind).toBe("blocked");
    expect(fetcher.calls).toBe(2);

    const flush = await transport.flush();
    expect(flush.pendingExactRetryCount).toBe(0);
    expect(flush.retryOutcomes[0]?.delivery.failure).toMatchObject({
      kind: "http",
      status: 422,
      retryable: false,
    });

    const terminalRetry = await transport.send(terminal);

    expect(terminalRetry.delivered).toBe(true);
    expect(fetcher.bodies()[3]).toMatchObject({
      sessionId: "sess_terminal_retry",
      drainSequence: 9,
      end: { reason: "call_ended" },
      resync: {
        missedFromSequence: 8,
        missedThroughSequence: 8,
        reason: "upload_failed_payload_dropped",
      },
    });
    expect(terminal.resync).toBeUndefined();

    const next = await transport.send(
      payload({
        captureVersion: 2,
        sessionId: "sess_terminal_retry",
        drainSequence: 10,
      }),
    );
    expect(next.delivered).toBe(true);
    expect(fetcher.bodies()[4]!.resync).toBeUndefined();
  });

  it("replays an ambiguous v2 request byte-for-byte before advancing", async () => {
    const fetcher = new FakeFetch([503, 202, 202]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });
    const first = payload({ captureVersion: 2, drainSequence: 1 });
    const second = payload({ captureVersion: 2, drainSequence: 2 });

    const firstResult = await transport.send(first);
    expect(firstResult.failure).toMatchObject({
      kind: "http",
      status: 503,
      retryable: true,
      retryRetained: true,
    });
    expect((await transport.send(second)).delivered).toBe(true);

    const bodies = fetcher.bodies();
    expect(bodies).toHaveLength(3);
    expect(fetcher.requests[0]!.init.body).toBe(fetcher.requests[1]!.init.body);
    expect(bodies[2]!.drainSequence).toBe(2);
    expect(bodies[2]!.resync).toBeUndefined();
  });

  it("keeps queued request identity and headers from the send-time snapshot", async () => {
    let calls = 0;
    let releaseFirst: ((response: { ok: boolean; status: number }) => void) | undefined;
    const bodies: string[] = [];
    const traceparents: string[] = [];
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      maxAttempts: 1,
      fetch: (_url, init) => {
        bodies.push(init.body);
        traceparents.push(init.headers.traceparent ?? "");
        calls += 1;
        if (calls === 1) {
          return new Promise((resolve) => {
            releaseFirst = resolve;
          });
        }
        return Promise.resolve({ ok: true, status: 202 });
      },
    });
    const firstPayload = payload({
      captureVersion: 2,
      sessionId: "sess_original",
      drainSequence: 1,
    });
    const firstTraceparent = firstPayload.traceContext.traceparent;

    const firstSend = transport.send(firstPayload);
    firstPayload.sessionId = "sess_mutated";
    firstPayload.drainSequence = 9;
    firstPayload.traceContext.traceparent = `00-${"c".repeat(32)}-${"d".repeat(16)}-01`;
    releaseFirst?.({ ok: false, status: 503 });
    expect((await firstSend).failure?.retryRetained).toBe(true);
    await transport.send(
      payload({
        captureVersion: 2,
        sessionId: "sess_original",
        drainSequence: 2,
      }),
    );

    expect(calls).toBe(3);
    expect(bodies[0]).toBe(bodies[1]);
    expect(JSON.parse(bodies[0]!).sessionId).toBe("sess_original");
    expect(JSON.parse(bodies[1]!).drainSequence).toBe(1);
    expect(traceparents[0]).toBe(firstTraceparent);
    expect(traceparents[1]).toBe(firstTraceparent);
  });

  it("holds later drains behind an unresolved exact retry, then resyncs skipped drains", async () => {
    const fetcher = new FakeFetch([503, 503, 202, 202]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
    });
    const first = payload({ captureVersion: 2, drainSequence: 1 });

    expect((await transport.send(first)).failure?.retryRetained).toBe(true);
    const blocked = await transport.send(
      payload({ captureVersion: 2, drainSequence: 2 }),
    );
    expect(blocked.failure).toMatchObject({ kind: "blocked", retryable: false });
    expect(
      (await transport.send(payload({ captureVersion: 2, drainSequence: 3 }))).delivered,
    ).toBe(true);

    const bodies = fetcher.bodies();
    expect(bodies).toHaveLength(4);
    expect(bodies[0]).toEqual(bodies[1]);
    expect(bodies[1]).toEqual(bodies[2]);
    expect(bodies[3]!.drainSequence).toBe(3);
    expect(bodies[3]!.resync).toEqual({
      missedFromSequence: 2,
      missedThroughSequence: 2,
      reason: "upload_failed_payload_dropped",
    });
  });

  it("fails closed for untracked sessions when the recovery ledger reaches capacity", async () => {
    const fetcher = new FakeFetch([413, 413, 201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      maxRecoverySessions: 1,
    });

    await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_a", drainSequence: 1 }),
    );
    const overflow = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_b", drainSequence: 1 }),
    );
    const recovered = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_a", drainSequence: 2 }),
    );

    expect(overflow.failure?.resyncTracked).toBe(false);
    expect(recovered.delivered).toBe(true);
    expect(fetcher.bodies()[2]!.resync).toEqual({
      missedFromSequence: 1,
      missedThroughSequence: 1,
      reason: "upload_failed_payload_dropped",
    });
    expect(fetcher.calls).toBe(3);
  });

  it("flushes retained exact retries after another session saturates recovery state", async () => {
    const fetcher = new FakeFetch([503, 503, 202]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      maxRecoverySessions: 1,
    });

    const retained = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_a", drainSequence: 1 }),
    );
    const unretained = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_b", drainSequence: 1 }),
    );
    await transport.flush();

    expect(retained.failure?.retryRetained).toBe(true);
    expect(unretained.failure?.retryRetained).toBe(false);
    expect(fetcher.calls).toBe(3);
    expect(fetcher.requests[0]!.init.body).toBe(fetcher.requests[2]!.init.body);
  });

  it("does not advance after replay clears its only recovery state under saturation", async () => {
    const fetcher = new FakeFetch([503, 503, 202, 503]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      maxRecoverySessions: 1,
    });

    await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_a", drainSequence: 1 }),
    );
    await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_b", drainSequence: 1 }),
    );
    const next = await transport.send(
      payload({ captureVersion: 2, sessionId: "sess_a", drainSequence: 2 }),
    );

    expect(next.failure).toMatchObject({ kind: "blocked", resyncTracked: false });
    expect(fetcher.calls).toBe(3);
    expect(fetcher.requests[0]!.init.body).toBe(fetcher.requests[2]!.init.body);
  });

  it("can flush an in-flight ambiguous request after stop closes admission", async () => {
    let calls = 0;
    let releaseFirst: ((response: { ok: boolean; status: number }) => void) | undefined;
    let startedFirst: (() => void) | undefined;
    const firstStarted = new Promise<void>((resolve) => {
      startedFirst = resolve;
    });
    const bodies: string[] = [];
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      maxAttempts: 1,
      fetch: async (_url, init) => {
        bodies.push(init.body);
        calls += 1;
        if (calls === 1) {
          startedFirst?.();
          return new Promise((resolve) => {
            releaseFirst = resolve;
          });
        }
        return { ok: true, status: 202 };
      },
    });

    const sending = transport.send(payload({ captureVersion: 2, drainSequence: 1 }));
    await firstStarted;
    transport.stop();
    releaseFirst?.({ ok: false, status: 503 });
    const result = await sending;
    await transport.flush();

    expect(result.failure?.retryRetained).toBe(true);
    expect(calls).toBe(2);
    expect(bodies[0]).toBe(bodies[1]);
  });

  it("resolves the send when a failure observer throws", async () => {
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([400]).fetch,
      onFailure: () => {
        throw new Error("metrics unavailable");
      },
    });

    const result = await transport.send(payload());

    expect(result.failure?.status).toBe(400);
  }, 250);

  it("retains an ambiguous v2 request when its failure observer throws", async () => {
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([503]).fetch,
      maxAttempts: 1,
      onFailure: () => {
        throw new Error("metrics unavailable");
      },
    });

    const result = await transport.send(payload({ captureVersion: 2, drainSequence: 1 }));

    expect(result.failure).toMatchObject({
      status: 503,
      retryRetained: true,
    });
  }, 250);

  it("resolves the send when the coverage sink throws", async () => {
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([400]).fetch,
      coverage: {
        recordCoverage: () => {
          throw new Error("recorder unavailable");
        },
      },
    });

    const result = await transport.send(payload());

    expect(result.failure?.status).toBe(400);
  }, 250);

  it("attaches a resync after the queue drops an older v2 drain", async () => {
    const gate = gatedFetch();
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: gate.fetch,
      maxQueuedPayloads: 1,
    });
    const first = transport.send(payload({ captureVersion: 2, drainSequence: 1 }));
    const dropped = transport.send(payload({ captureVersion: 2, drainSequence: 2 }));
    const next = transport.send(payload({ captureVersion: 2, drainSequence: 3 }));

    expect((await dropped).failure).toMatchObject({ kind: "queue_overflow" });
    gate.open();
    expect((await first).delivered).toBe(true);
    expect((await next).delivered).toBe(true);
    expect(gate.bodies[1]!.resync).toEqual({
      missedFromSequence: 2,
      missedThroughSequence: 2,
      reason: "upload_failed_payload_dropped",
    });
  });

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

  it("the next continuous drain carries resync after an upload failure", async () => {
    const recorder = new EarshotBrowserRecorder({
      captureVersion: 2,
      random: sequentialRandom(),
    });
    const fetcher = new FakeFetch([413, 201]);
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: fetcher.fetch,
      maxAttempts: 1,
      sleep: new FakeSleep().sleep,
      coverage: recorder,
    });

    await transport.send(recorder.drain());
    await transport.send(recorder.drain());

    expect(fetcher.bodies()[1]!.drainSequence).toBe(2);
    expect(fetcher.bodies()[1]!.resync).toEqual({
      missedFromSequence: 1,
      missedThroughSequence: 1,
      reason: "upload_failed_payload_dropped",
    });
    expect(fetcher.bodies()[1]!.coverage).toContainEqual({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: 0,
    });
  });

  it("returns the opaque server call id needed to seal an ended v2 session", async () => {
    const recorder = new EarshotBrowserRecorder({
      captureVersion: 2,
      sessionId: "sess_final",
      random: sequentialRandom(),
    });
    const accepted: unknown[] = [];
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: async () => ({
        ok: true,
        status: 202,
        json: async () => ({ call_id: "call_opaque_server_id", finalized: true }),
      }),
      onAccepted: (acknowledgement) => {
        accepted.push(acknowledgement);
      },
    });

    const result = await transport.send(recorder.endCall());

    expect(result.callId).toBe("call_opaque_server_id");
    expect(result.finalized).toBe(true);
    expect(accepted).toEqual([
      {
        projectId: "project_test",
        authContextId: "authctx_test",
        sessionId: "sess_final",
        captureVersion: 2,
        drainSequence: 1,
        callId: "call_opaque_server_id",
        finalized: true,
      },
    ]);
  });

  it("keeps an async acceptance observer rejection from changing delivery", async () => {
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([202]).fetch,
      onAccepted: async () => {
        throw new Error("host notification unavailable");
      },
    });

    const result = await transport.send(payload());

    expect(result.delivered).toBe(true);
    expect(result.acceptedObserverFailed).toBe(true);
  });

  it("waits for the accepted observer to persist its acknowledgement", async () => {
    let finishObserver: (() => void) | undefined;
    let observerStarted: (() => void) | undefined;
    const started = new Promise<void>((resolve) => {
      observerStarted = resolve;
    });
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      fetch: new FakeFetch([202]).fetch,
      onAccepted: () => {
        observerStarted?.();
        return new Promise<void>((resolve) => {
          finishObserver = resolve;
        });
      },
    });

    let settled = false;
    const sending = transport.send(payload()).then((result) => {
      settled = true;
      return result;
    });
    await started;
    expect(settled).toBe(false);
    finishObserver?.();
    expect((await sending).delivered).toBe(true);
  });

  it("surfaces acceptance observer failures when a flush retry succeeds", async () => {
    const acceptedFailures: unknown[] = [];
    let calls = 0;
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      maxAttempts: 1,
      fetch: async () => {
        calls += 1;
        return calls === 1
          ? { ok: false, status: 503 }
          : {
              ok: true,
              status: 202,
              json: async () => ({ call_id: "call_retry", finalized: true }),
            };
      },
      onAccepted: async () => {
        throw new Error("outbox unavailable");
      },
      onAcceptedFailure: async (acknowledgement) => {
        acceptedFailures.push(acknowledgement);
        throw new Error("repair notification unavailable");
      },
    });

    const first = await transport.send(payload({ captureVersion: 2, drainSequence: 1 }));
    expect(first.failure?.retryRetained).toBe(true);
    const flushResult = await transport.flush();

    expect(flushResult.acceptedObserverFailed).toBe(true);
    expect(acceptedFailures).toEqual([
      {
        projectId: "project_test",
        authContextId: "authctx_test",
        sessionId: "sess_abc",
        captureVersion: 2,
        drainSequence: 1,
        callId: "call_retry",
        finalized: true,
      },
    ]);
  });

  it("notifies the host when flush later accepts a finalized request", async () => {
    const recorder = new EarshotBrowserRecorder({
      captureVersion: 2,
      sessionId: "sess_final_retry",
      random: sequentialRandom(),
    });
    const accepted: unknown[] = [];
    let calls = 0;
    const transport = createCaptureTransport({
      endpoint: ENDPOINT,
      maxAttempts: 1,
      fetch: async () => {
        calls += 1;
        return calls === 1
          ? { ok: false, status: 503 }
          : {
              ok: true,
              status: 202,
              json: async () => ({ call_id: "call_after_retry", finalized: true }),
            };
      },
      onAccepted: (acknowledgement) => {
        accepted.push(acknowledgement);
      },
    });

    const initial = await transport.send(recorder.endCall());
    expect(initial.failure?.retryRetained).toBe(true);
    const flushResult = await transport.flush();

    expect(accepted).toEqual([
      {
        projectId: "project_test",
        authContextId: "authctx_test",
        sessionId: "sess_final_retry",
        captureVersion: 2,
        drainSequence: 1,
        callId: "call_after_retry",
        finalized: true,
      },
    ]);
    expect(flushResult).toMatchObject({
      pendingExactRetryCount: 0,
      retryOutcomes: [
        {
          sessionId: "sess_final_retry",
          drainSequence: 1,
          delivery: { delivered: true, status: 202 },
        },
      ],
    });
  });
});
