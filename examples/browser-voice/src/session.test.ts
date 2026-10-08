import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CaptureAcknowledgement,
  CaptureDeliveryResult,
  CaptureFlushResult,
} from "@earshot/browser";

const mocks = vi.hoisted(() => {
  const finalPayload = { captureVersion: 2, sessionId: "sess_test", drainSequence: 9 };
  const observerPayload = { captureVersion: 2, sessionId: "sess_test", drainSequence: 8 };
  const recorder = {
    sessionId: "sess_test",
    traceContext: vi.fn(() => ({ traceparent: "00-trace" })),
    drain: vi.fn(() => observerPayload),
    endCall: vi.fn(() => finalPayload),
    stop: vi.fn(),
  };
  const transport = {
    send: vi.fn(async (): Promise<CaptureDeliveryResult> => ({
      delivered: true,
      attempts: 1,
      status: 202,
    })),
    flush: vi.fn(async (): Promise<CaptureFlushResult> => ({
      acceptedObserverFailed: false,
      pendingExactRetryCount: 0,
      retryOutcomes: [],
    })),
    stop: vi.fn(),
  };
  return {
    finalPayload,
    observerPayload,
    recorder,
    transport,
    createBrowserRecorder: vi.fn(() => recorder),
    createCaptureTransport: vi.fn((_options: unknown) => transport),
  };
});

vi.mock("@earshot/browser", () => ({
  CONTINUOUS_CAPTURE_VERSION: 2,
  createBrowserRecorder: mocks.createBrowserRecorder,
  createCaptureTransport: mocks.createCaptureTransport,
}));

import {
  CaptureSession,
  isFinalPayloadReplaceable,
  isTerminalCapture413,
} from "./session.js";

describe("CaptureSession call lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.recorder.traceContext.mockReturnValue({ traceparent: "00-trace" });
    mocks.recorder.drain.mockReturnValue(mocks.observerPayload);
    mocks.recorder.endCall.mockReturnValue(mocks.finalPayload);
    mocks.transport.send.mockResolvedValue({ delivered: true, attempts: 1, status: 202 });
  });

  it("finalizes and delivers one final batch when the actual call ends", async () => {
    const onAccepted = vi.fn((_acknowledgement: CaptureAcknowledgement) => undefined);
    const onAcceptedFailure = vi.fn(
      (_acknowledgement: CaptureAcknowledgement) => undefined,
    );
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
      onAccepted,
      onAcceptedFailure,
    });

    await session.endCall();
    await session.endCall();

    expect(mocks.recorder.endCall).toHaveBeenCalledTimes(1);
    expect(mocks.transport.send).toHaveBeenCalledTimes(1);
    expect(mocks.transport.send).toHaveBeenCalledWith(mocks.finalPayload);
    expect(mocks.transport.flush).toHaveBeenCalledTimes(1);
    expect(mocks.transport.stop).toHaveBeenCalledTimes(1);
    const transportOptions = mocks.createCaptureTransport.mock.calls[0]?.[0] as
      | {
          onAccepted?: unknown;
          onAcceptedFailure?: (acknowledgement: CaptureAcknowledgement) => void;
        }
      | undefined;
    expect(transportOptions?.onAccepted).toBe(onAccepted);
    const acceptedAcknowledgement: CaptureAcknowledgement = {
      projectId: "project_test",
      authContextId: "authctx_test",
      sessionId: "sess_test",
      captureVersion: 2,
      drainSequence: 9,
    };
    transportOptions?.onAcceptedFailure?.(acceptedAcknowledgement);
    expect(onAcceptedFailure).toHaveBeenCalledWith(acceptedAcknowledgement);
  });

  it("stops observation without falsely marking the call ended", async () => {
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    await session.stop();

    expect(mocks.recorder.stop).toHaveBeenCalledTimes(1);
    expect(mocks.recorder.endCall).not.toHaveBeenCalled();
    expect(mocks.transport.send).toHaveBeenCalledWith(mocks.observerPayload);
  });

  it("returns an unresolved final retry to the caller", async () => {
    mocks.transport.send.mockResolvedValue({
      delivered: false,
      attempts: 1,
      failure: {
        kind: "http",
        retryable: true,
        attempts: 1,
        droppedObservations: 0,
        sessionId: "sess_test",
        retryRetained: true,
      },
    });
    mocks.transport.flush.mockResolvedValue({
      acceptedObserverFailed: false,
      pendingExactRetryCount: 1,
      retryOutcomes: [
        {
          sessionId: "sess_test",
          drainSequence: 9,
          delivery: {
            delivered: false,
            attempts: 1,
            failure: {
              kind: "http",
              retryable: true,
              attempts: 1,
              droppedObservations: 0,
              sessionId: "sess_test",
              retryRetained: true,
            },
          },
        },
      ],
    });
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const result = await session.endCall();

    expect(result).toMatchObject({
      payload: mocks.finalPayload,
      delivery: { delivered: false, failure: { retryRetained: true } },
      acceptedObserverFailed: false,
      pendingExactRetryCount: 1,
    });
  });

  it("sends a smaller terminal replacement after a permanent 413", async () => {
    const finalPayload = {
      ...mocks.finalPayload,
      traceContext: { traceparent: "00-trace", traceId: "trace", spanId: "span" },
      clockDomain: {
        id: "clk_test",
        kind: "browser_monotonic" as const,
        unit: "ms" as const,
        uncertaintyMs: 1,
        wallOriginMs: 1_000,
      },
      snapshots: Array.from({ length: 50 }, (_, index) => ({
        timestamp_ms: index + 1,
        stats: {
          [`inbound_${index}`]: {
            type: "inbound-rtp",
            kind: "audio",
            packetsReceived: index,
          },
        },
      })),
      deviceEvents: Array.from({ length: 50 }, (_, index) => ({
        type: "audiocontext_state",
        timestamp_ms: index + 1,
        state: "running",
      })),
      coverage: [],
      capturerStartedAtMs: 0,
      end: { reason: "call_ended" as const, timestampMs: 3 },
    };
    mocks.recorder.endCall.mockReturnValue(finalPayload);
    mocks.transport.send
      .mockReset()
      .mockResolvedValueOnce({
        delivered: false,
        attempts: 1,
        status: 413,
        failure: {
          kind: "http",
          status: 413,
          retryable: false,
          attempts: 1,
          droppedObservations: 100,
          sessionId: "sess_test",
        },
      })
      .mockResolvedValueOnce({ delivered: true, attempts: 1, status: 202 });
    mocks.transport.flush.mockReset().mockResolvedValue({
      acceptedObserverFailed: false,
      pendingExactRetryCount: 0,
      retryOutcomes: [],
    });

    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const rejected = await session.endCall();
    expect(rejected.delivery).toMatchObject({ delivered: false, status: 413 });
    expect(isTerminalCapture413(rejected)).toBe(true);
    expect(isFinalPayloadReplaceable(rejected)).toBe(true);
    const emptyTerminal = {
      ...rejected,
      payload: { ...rejected.payload, snapshots: [], deviceEvents: [] },
    };
    expect(isTerminalCapture413(emptyTerminal)).toBe(true);
    expect(isFinalPayloadReplaceable(emptyTerminal)).toBe(false);
    expect(mocks.transport.stop).not.toHaveBeenCalled();

    await expect(
      session.retryFinalWithReducedPayload({
        ...finalPayload,
        drainSequence: finalPayload.drainSequence + 1,
        snapshots: [],
        deviceEvents: [],
      }),
    ).rejects.toThrow("preserve session identity");
    expect(mocks.transport.send).toHaveBeenCalledTimes(1);

    const repaired = await session.retryFinalWithReducedPayload();

    expect(mocks.transport.send).toHaveBeenCalledTimes(2);
    expect(mocks.transport.send).toHaveBeenNthCalledWith(1, finalPayload);
    const sendCalls = mocks.transport.send.mock.calls as unknown as [unknown][];
    const replacement = sendCalls[1]?.[0] as typeof finalPayload;
    expect(replacement).toMatchObject({
      captureVersion: finalPayload.captureVersion,
      sessionId: finalPayload.sessionId,
      drainSequence: finalPayload.drainSequence,
      traceContext: finalPayload.traceContext,
      clockDomain: finalPayload.clockDomain,
      end: finalPayload.end,
      snapshots: [],
      deviceEvents: [],
      coverage: [
        {
          signal: "capture.upload",
          availability: "partial",
          reason: "upload_failed_payload_dropped",
          droppedCount: 100,
        },
      ],
    });
    expect(repaired).toMatchObject({
      delivery: { delivered: true, status: 202 },
      pendingExactRetryCount: 0,
    });
    expect(mocks.transport.stop).toHaveBeenCalledTimes(1);
  });

  it("surfaces observer failure when a final batch is accepted during flush", async () => {
    mocks.transport.send.mockResolvedValue({
      delivered: false,
      attempts: 1,
      failure: {
        kind: "http",
        retryable: true,
        attempts: 1,
        droppedObservations: 0,
        sessionId: "sess_test",
        retryRetained: true,
      },
    });
    mocks.transport.flush.mockResolvedValue({
      acceptedObserverFailed: true,
      pendingExactRetryCount: 0,
      retryOutcomes: [
        {
          sessionId: "sess_test",
          drainSequence: 9,
          delivery: { delivered: true, attempts: 1, status: 202 },
        },
      ],
    });
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const result = await session.endCall();

    expect(result).toMatchObject({
      delivery: { delivered: true, status: 202 },
      acceptedObserverFailed: true,
      pendingExactRetryCount: 0,
    });
  });

  it("resends the terminal batch after flush recovers its blocked predecessor", async () => {
    mocks.transport.send
      .mockResolvedValueOnce({
        delivered: false,
        attempts: 0,
        failure: {
          kind: "blocked",
          retryable: false,
          attempts: 0,
          droppedObservations: 0,
          sessionId: "sess_test",
        },
      })
      .mockResolvedValueOnce({ delivered: true, attempts: 1, status: 202 });
    mocks.transport.flush
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: { delivered: true, attempts: 1, status: 202 },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [],
      });
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const result = await session.endCall();

    expect(mocks.transport.send).toHaveBeenCalledTimes(2);
    expect(mocks.transport.send).toHaveBeenNthCalledWith(1, mocks.finalPayload);
    expect(mocks.transport.send).toHaveBeenNthCalledWith(2, mocks.finalPayload);
    expect(mocks.transport.flush).toHaveBeenCalledTimes(2);
    expect(result).toMatchObject({
      delivery: { delivered: true, status: 202 },
      pendingExactRetryCount: 0,
    });
  });

  it("resends the terminal batch after a permanent predecessor rejection", async () => {
    mocks.transport.send
      .mockResolvedValueOnce({
        delivered: false,
        attempts: 0,
        failure: {
          kind: "blocked",
          retryable: false,
          attempts: 0,
          droppedObservations: 0,
          sessionId: "sess_test",
        },
      })
      .mockResolvedValueOnce({ delivered: true, attempts: 1, status: 202 });
    mocks.transport.flush
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 1,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: {
              delivered: false,
              attempts: 2,
              failure: {
                kind: "http",
                retryable: true,
                attempts: 2,
                droppedObservations: 1,
                sessionId: "sess_test",
                status: 503,
                retryRetained: true,
              },
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: {
              delivered: false,
              attempts: 2,
              status: 422,
              failure: {
                kind: "http",
                retryable: false,
                attempts: 2,
                droppedObservations: 1,
                sessionId: "sess_test",
                status: 422,
                retryRetained: false,
              },
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [],
      });
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const initial = await session.endCall();
    expect(initial).toMatchObject({
      delivery: { delivered: false, failure: { kind: "blocked" } },
      pendingExactRetryCount: 1,
    });

    const finalized = await session.retryPending();

    expect(mocks.transport.send).toHaveBeenCalledTimes(2);
    expect(mocks.transport.send).toHaveBeenNthCalledWith(2, mocks.finalPayload);
    expect(finalized).toMatchObject({
      delivery: { delivered: true, status: 202 },
      pendingExactRetryCount: 0,
    });
    expect(mocks.transport.stop).toHaveBeenCalledTimes(1);
  });

  it("keeps a failed finalization retryable until a predecessor and terminal batch land", async () => {
    mocks.transport.send
      .mockResolvedValueOnce({
        delivered: false,
        attempts: 0,
        failure: {
          kind: "blocked",
          retryable: false,
          attempts: 0,
          droppedObservations: 0,
          sessionId: "sess_test",
        },
      })
      .mockResolvedValueOnce({ delivered: true, attempts: 1, status: 202 });
    mocks.transport.flush
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 1,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: {
              delivered: false,
              attempts: 1,
              failure: {
                kind: "http",
                retryable: true,
                attempts: 1,
                droppedObservations: 0,
                sessionId: "sess_test",
                retryRetained: true,
              },
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 1,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: {
              delivered: false,
              attempts: 1,
              failure: {
                kind: "http",
                retryable: true,
                attempts: 1,
                droppedObservations: 0,
                sessionId: "sess_test",
                retryRetained: true,
              },
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [
          {
            sessionId: "sess_test",
            drainSequence: 8,
            delivery: { delivered: true, attempts: 1, status: 202 },
          },
        ],
      })
      .mockResolvedValueOnce({
        acceptedObserverFailed: false,
        pendingExactRetryCount: 0,
        retryOutcomes: [],
      });
    const session = new CaptureSession({
      endpoint: {
        endpoint: "/api/earshot/v1/capture",
        projectId: "project_test",
        getAuthContextId: () => "authctx_test",
      },
      log: vi.fn(),
    });

    const initial = await session.endCall();

    expect(initial).toMatchObject({
      delivery: { delivered: false, failure: { kind: "blocked" } },
      pendingExactRetryCount: 1,
    });
    expect(mocks.transport.stop).not.toHaveBeenCalled();

    const stillPending = await session.retryPending();

    expect(stillPending.pendingExactRetryCount).toBe(1);
    expect(mocks.transport.send).toHaveBeenCalledTimes(1);
    expect(mocks.transport.stop).not.toHaveBeenCalled();

    const recovered = await session.retryPending();

    expect(mocks.transport.send).toHaveBeenCalledTimes(2);
    expect(mocks.transport.send).toHaveBeenNthCalledWith(2, mocks.finalPayload);
    expect(recovered).toMatchObject({
      delivery: { delivered: true, status: 202 },
      pendingExactRetryCount: 0,
    });
    expect(mocks.transport.stop).toHaveBeenCalledTimes(1);
  });
});
