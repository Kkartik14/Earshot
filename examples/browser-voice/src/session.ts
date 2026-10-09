/**
 * The capture glue: everything both modes (loopback + OpenAI) share.
 *
 * A `CaptureSession` owns one `EarshotBrowserRecorder` and one
 * `EarshotCaptureTransport` from `@earshot/browser` and drives the REAL W3C code
 * paths the reviewer flagged as unexercised:
 *
 *  - `requestMicrophone(navigator.mediaDevices, …)` — the real `getUserMedia`
 *    microphone-permission path (granted/denied), not a synthesised source.
 *  - `observeMediaDevices(navigator.mediaDevices, { permissions })` — the real
 *    Permissions API `microphone` descriptor and `devicechange`.
 *  - `attachPeerConnection(pc)` — real `RTCPeerConnection.getStats()` sampling.
 *  - `attachAudioContext(ctx)` — real `AudioContext` state / `outputLatency` /
 *    `getOutputTimestamp()` render-queue / `sinkchange` observation.
 *
 * It NEVER fabricates a metric. Every value comes from a real browser object
 * handed to the SDK; where the running browser does not expose a signal, the SDK
 * records its own coverage note and this session does nothing to paper over it.
 *
 * Delivery goes to the earshot backend's `POST /v1/capture` via the SDK's
 * transport. The recorder is wired as the transport's coverage sink, so a batch
 * that fails to upload is declared as coverage on the next drain rather than
 * silently lost.
 */

import {
  CONTINUOUS_CAPTURE_VERSION,
  createBrowserRecorder,
  createCaptureTransport,
} from "@earshot/browser";
import type {
  CaptureAcknowledgement,
  CaptureDeliveryResult,
  CaptureDeliveryFailure,
  CaptureFlushResult,
  CapturePayload,
  EarshotBrowserRecorder,
  EarshotCaptureTransport,
} from "@earshot/browser";

/** Where and how to authenticate the `POST /v1/capture` call. */
export interface CaptureEndpointConfig {
  /** Absolute or same-origin-relative host BFF endpoint. No default. */
  endpoint: string;
  /** Host-session CSRF token, required by cookie-authenticated BFFs. */
  csrfToken?: string;
  /** Required project assertion header. */
  projectId: string;
  /** Host's opaque version of the effective project/user grant. */
  getAuthContextId: () => string;
}

export interface CaptureSessionOptions {
  endpoint: CaptureEndpointConfig;
  /** Correlation id; the recorder mints one when omitted. */
  sessionId?: string;
  /** Join an existing app trace instead of minting one. */
  traceparent?: string;
  /** How often to drain the buffers and POST a batch (default 10s). */
  drainIntervalMs?: number;
  /** Human-readable progress log. */
  log: (message: string) => void;
  /** Called with every drained payload (before it is sent), for inspection. */
  onDrain?: (payload: CapturePayload) => void;
  /** Called for every batch the transport gives up on. */
  onFailure?: (failure: CaptureDeliveryFailure) => void;
  /** Called after an accepted drain, including an exact retry that later succeeds. */
  onAccepted?: (acknowledgement: CaptureAcknowledgement) => void | Promise<void>;
  /** Called when the host acceptance callback fails, including on a flush retry. */
  onAcceptedFailure?: (acknowledgement: CaptureAcknowledgement) => void | Promise<void>;
}

/** Final delivery state returned by `stop()`, `endCall()`, and `retryPending()`. */
export interface CaptureSessionFinishResult {
  payload: CapturePayload;
  /** Result of the final POST, or its matching bounded retry when one was attempted. */
  delivery: CaptureDeliveryResult;
  /** Whether any accepted browser acknowledgement observer failed during delivery. */
  acceptedObserverFailed: boolean;
  /** Exact requests still retained in this transport after its bounded retry pass. */
  pendingExactRetryCount: number;
}

export function isTerminalCapture413(result: CaptureSessionFinishResult): boolean {
  return (
    !result.delivery.delivered &&
    result.delivery.status === 413 &&
    result.pendingExactRetryCount === 0 &&
    result.payload.end !== undefined
  );
}

export function isFinalPayloadReplaceable(result: CaptureSessionFinishResult): boolean {
  if (!isTerminalCapture413(result)) return false;
  try {
    createReducedFinalPayload(result.payload);
    return true;
  } catch {
    return false;
  }
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isOrderedSubset<T>(candidate: T[], original: T[]): boolean {
  let candidateIndex = 0;
  for (const item of original) {
    if (sameJson(candidate[candidateIndex], item)) candidateIndex += 1;
    if (candidateIndex === candidate.length) return true;
  }
  return candidateIndex === candidate.length;
}

function replacementWithLossCoverage(
  payload: CapturePayload,
  droppedObservations: number,
): CapturePayload {
  const coverage = [...payload.coverage];
  const uploadIndex = coverage.findIndex((note) => note.signal === "capture.upload");
  if (uploadIndex === -1) {
    coverage.push({
      signal: "capture.upload",
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      droppedCount: droppedObservations,
    });
  } else {
    const existing = coverage[uploadIndex];
    if (!existing) throw new Error("capture upload coverage note is missing");
    const priorCount = existing.droppedCount;
    const total = priorCount === undefined ? undefined : priorCount + droppedObservations;
    coverage[uploadIndex] = {
      ...existing,
      availability: "partial",
      reason: "upload_failed_payload_dropped",
      ...(total !== undefined && total <= 2_147_483_647
        ? { droppedCount: total }
        : { droppedCount: undefined }),
    };
  }
  return { ...payload, coverage };
}

function encodedPayloadSize(payload: CapturePayload): number {
  const serialized = JSON.stringify(payload);
  if (serialized === undefined) {
    throw new TypeError("capture replacement did not serialize to JSON");
  }
  return new TextEncoder().encode(serialized).byteLength;
}

function createReducedFinalPayload(
  original: CapturePayload,
  candidate?: CapturePayload,
): CapturePayload {
  if (!original.end || original.captureVersion !== CONTINUOUS_CAPTURE_VERSION) {
    throw new Error("only a terminal continuous capture can be replaced");
  }
  const replacement = candidate ?? {
    ...original,
    snapshots: [],
    deviceEvents: [],
  };
  if (
    replacement.captureVersion !== original.captureVersion ||
    replacement.sessionId !== original.sessionId ||
    replacement.drainSequence !== original.drainSequence ||
    !sameJson(replacement.traceContext, original.traceContext) ||
    !sameJson(replacement.clockDomain, original.clockDomain) ||
    replacement.capturerStartedAtMs !== original.capturerStartedAtMs ||
    !sameJson(replacement.end, original.end) ||
    !sameJson(replacement.resync, original.resync) ||
    !sameJson(replacement.coverage, original.coverage) ||
    !isOrderedSubset(replacement.snapshots, original.snapshots) ||
    !isOrderedSubset(replacement.deviceEvents, original.deviceEvents)
  ) {
    throw new Error(
      "a final replacement must preserve session identity, terminal metadata, coverage, and observation order",
    );
  }

  const lostObservations =
    original.snapshots.length +
    original.deviceEvents.length -
    replacement.snapshots.length -
    replacement.deviceEvents.length;
  if (lostObservations === 0) {
    throw new Error("a final replacement must remove at least one observation");
  }
  const reduced = replacementWithLossCoverage(replacement, lostObservations);
  if (encodedPayloadSize(reduced) >= encodedPayloadSize(original)) {
    throw new Error("the reduced final payload is not smaller than the rejected payload");
  }
  return reduced;
}

const DEFAULT_DRAIN_INTERVAL_MS = 10_000;

export class CaptureSession {
  readonly recorder: EarshotBrowserRecorder;
  private readonly transport: EarshotCaptureTransport;
  private readonly log: (message: string) => void;
  private readonly onDrain?: (payload: CapturePayload) => void;
  private readonly drainIntervalMs: number;
  private timer: ReturnType<typeof setInterval> | undefined;
  private finishPromise: Promise<CaptureSessionFinishResult> | undefined;
  private retryPromise: Promise<CaptureSessionFinishResult> | undefined;
  private latestFinishResult: CaptureSessionFinishResult | undefined;

  constructor(options: CaptureSessionOptions) {
    this.log = options.log;
    this.onDrain = options.onDrain;
    this.drainIntervalMs = options.drainIntervalMs ?? DEFAULT_DRAIN_INTERVAL_MS;

    // `captureVersion: 2` (continuous capture) is exposed by this SDK, so each
    // drain carries a monotonic drainSequence under one stable session and the
    // server accumulates the whole call instead of a per-drain incident.
    this.recorder = createBrowserRecorder({
      captureVersion: CONTINUOUS_CAPTURE_VERSION,
      sessionId: options.sessionId,
      traceparent: options.traceparent,
    });

    this.transport = createCaptureTransport({
      endpoint: options.endpoint.endpoint,
      csrfToken: options.endpoint.csrfToken,
      projectId: options.endpoint.projectId,
      getAuthContextId: options.endpoint.getAuthContextId,
      // An undelivered batch becomes coverage on the next drain — never a silent
      // gap the server would read as a clean session.
      coverage: this.recorder,
      onFailure: (failure) => {
        this.log(
          `capture delivery gave up: kind=${failure.kind}` +
            `${failure.status === undefined ? "" : ` status=${failure.status}`}` +
            ` attempts=${failure.attempts} dropped=${failure.droppedObservations}`,
        );
        options.onFailure?.(failure);
      },
      onAccepted: options.onAccepted,
      onAcceptedFailure: (acknowledgement) => {
        this.log(
          "browser acknowledgement callback failed; the host BFF outbox remains authoritative",
        );
        return options.onAcceptedFailure?.(acknowledgement);
      },
    });

    this.log(
      `session ${this.recorder.sessionId} (captureVersion=${CONTINUOUS_CAPTURE_VERSION}, ` +
        `traceparent=${this.recorder.traceContext().traceparent})`,
    );
  }

  /**
   * Run the real `getUserMedia` microphone-permission path via the SDK and return
   * the live stream (or `null` if the user/agent denied it). The SDK records a
   * `granted` (with an opaque device hash) or `denied` permission event; this
   * method never throws.
   *
   * The SDK types the result structurally as `MediaStreamLike`; at runtime it is
   * the browser's real `MediaStream`, which the caller needs in full to add
   * tracks to a peer connection and to build an `AudioContext` source.
   */
  async requestMicrophone(
    constraints: MediaStreamConstraints,
  ): Promise<MediaStream | null> {
    this.log("requesting microphone (getUserMedia)…");
    const stream = await this.recorder.requestMicrophone(
      navigator.mediaDevices,
      constraints,
    );
    if (stream === null) {
      this.log("microphone denied — recorded as a 'denied' permission event");
      return null;
    }
    this.log("microphone granted — recorded a 'granted' permission event");
    return stream as MediaStream;
  }

  /**
   * Read + watch the real Permissions API `microphone` descriptor and
   * `devicechange`. Some browsers reject `query({ name: "microphone" })`; the SDK
   * records that as coverage rather than throwing.
   */
  async observeDevices(): Promise<void> {
    this.log("observing permissions + devicechange…");
    await this.recorder.observeMediaDevices(navigator.mediaDevices, {
      permissions: navigator.permissions,
    });
  }

  /** Sample real `RTCPeerConnection.getStats()` at `intervalMs` (default 1s). */
  attachPeerConnection(pc: RTCPeerConnection, intervalMs = 1000): void {
    this.recorder.attachPeerConnection(pc, { intervalMs });
    this.log(`sampling RTCPeerConnection.getStats() every ${intervalMs}ms`);
  }

  /**
   * Observe a real `AudioContext` render path: state changes, `outputLatency`,
   * the `getOutputTimestamp()` render-queue depth, and `sinkchange`. Anything the
   * platform does not expose becomes an SDK coverage note, not a guessed value.
   */
  attachAudioContext(ctx: AudioContext, renderTimingIntervalMs = 1000): void {
    this.recorder.attachAudioContext(ctx, { renderTimingIntervalMs });
    this.log(
      "observing AudioContext (state / outputLatency / render-queue / sinkchange)",
    );
  }

  /** Begin the periodic drain -> POST /v1/capture loop. */
  start(): void {
    if (this.finishPromise) {
      throw new Error("cannot start a finished capture session");
    }
    if (this.timer !== undefined) return;
    this.timer = setInterval(() => {
      void this.drainAndSend().catch(() => {
        this.log("capture drain failed before delivery");
      });
    }, this.drainIntervalMs);
    this.log(`draining + POSTing every ${this.drainIntervalMs}ms`);
  }

  private async drainAndSend(): Promise<void> {
    const payload = this.recorder.drain();
    this.notifyDrain(payload);
    const result = await this.transport.send(payload);
    this.reportDeliveryResult(result);
    if (result.delivered) {
      this.log(
        `batch delivered (status=${result.status}, ` +
          `drainSequence=${payload.drainSequence ?? "n/a"}, ` +
          `snapshots=${payload.snapshots.length}, events=${payload.deviceEvents.length})`,
      );
    }
  }

  /**
   * Stop observation without claiming that the call ended. Use `endCall()` when
   * the application knows the voice call itself is closed.
   */
  async stop(): Promise<CaptureSessionFinishResult> {
    return this.finish(false);
  }

  /** Finalize the actual call and deliver its final v2 batch exactly once. */
  async endCall(): Promise<CaptureSessionFinishResult> {
    return this.finish(true);
  }

  /** Retry retained delivery after stop/endCall while its transport state remains in memory. */
  retryPending(): Promise<CaptureSessionFinishResult> {
    if (this.retryPromise) return this.retryPromise;
    const finishPromise = this.finishPromise;
    if (!finishPromise) {
      return Promise.reject(new Error("cannot retry capture before stop or endCall"));
    }

    const retryPromise = (async () => {
      const firstResult = await finishPromise;
      const previous = this.latestFinishResult ?? firstResult;
      if (previous.pendingExactRetryCount === 0) return previous;

      const result = await this.reconcileFinalDelivery(
        previous.payload,
        previous.delivery,
        previous.acceptedObserverFailed,
        previous.payload.end?.reason === "call_ended",
      );
      this.latestFinishResult = result;
      if (result.pendingExactRetryCount === 0 && !isFinalPayloadReplaceable(result)) {
        this.transport.stop();
      }
      return result;
    })();
    this.retryPromise = retryPromise;
    return retryPromise.finally(() => {
      if (this.retryPromise === retryPromise) this.retryPromise = undefined;
    });
  }

  /**
   * Recover a terminal 413 by sending a smaller body at the same session and
   * drain sequence. By default this keeps identity, clock, end, and coverage
   * metadata while omitting snapshots and device events. A host may pass an
   * ordered subset of those observations to retain when a smaller body is
   * enough. The loss is declared as `capture.upload` coverage.
   */
  retryFinalWithReducedPayload(
    replacement?: CapturePayload,
  ): Promise<CaptureSessionFinishResult> {
    if (this.retryPromise) return this.retryPromise;
    const finishPromise = this.finishPromise;
    if (!finishPromise) {
      return Promise.reject(new Error("cannot replace capture before stop or endCall"));
    }

    const retryPromise = (async () => {
      const initial = await finishPromise;
      const previous = this.latestFinishResult ?? initial;
      if (!isFinalPayloadReplaceable(previous)) {
        throw new Error(
          "a smaller replacement is available only for a reducible terminal 413",
        );
      }
      const reducedPayload = createReducedFinalPayload(previous.payload, replacement);
      this.notifyDrain(reducedPayload);
      const delivery = await this.transport.send(reducedPayload);
      this.reportDeliveryResult(delivery);
      const result = await this.reconcileFinalDelivery(
        reducedPayload,
        delivery,
        delivery.acceptedObserverFailed === true,
        reducedPayload.end?.reason === "call_ended",
      );
      this.latestFinishResult = result;
      if (result.pendingExactRetryCount === 0 && !isFinalPayloadReplaceable(result)) {
        this.transport.stop();
      }
      return result;
    })();
    this.retryPromise = retryPromise;
    return retryPromise.finally(() => {
      if (this.retryPromise === retryPromise) this.retryPromise = undefined;
    });
  }

  private finish(callEnded: boolean): Promise<CaptureSessionFinishResult> {
    if (this.retryPromise) return this.retryPromise;
    if (this.finishPromise) {
      return this.finishPromise.then((result) => this.latestFinishResult ?? result);
    }
    if (this.timer !== undefined) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    const finalBatch = callEnded
      ? this.recorder.endCall()
      : (() => {
          this.recorder.stop();
          return this.recorder.drain();
        })();
    this.notifyDrain(finalBatch);
    this.finishPromise = this.deliverFinalBatch(finalBatch, callEnded).then((result) => {
      this.latestFinishResult = result;
      return result;
    });
    return this.finishPromise;
  }

  private async deliverFinalBatch(
    payload: CapturePayload,
    callEnded: boolean,
  ): Promise<CaptureSessionFinishResult> {
    try {
      const initialDelivery = await this.transport.send(payload);
      this.reportDeliveryResult(initialDelivery);
      const result = await this.reconcileFinalDelivery(
        payload,
        initialDelivery,
        initialDelivery.acceptedObserverFailed === true,
        callEnded,
      );
      this.latestFinishResult = result;
      if (result.pendingExactRetryCount === 0 && !isFinalPayloadReplaceable(result)) {
        this.transport.stop();
      }
      return result;
    } catch (error) {
      this.transport.stop();
      throw error;
    }
  }

  private async reconcileFinalDelivery(
    payload: CapturePayload,
    previousDelivery: CaptureDeliveryResult,
    previousObserverFailed: boolean,
    callEnded: boolean,
  ): Promise<CaptureSessionFinishResult> {
    let flushResult: CaptureFlushResult = await this.transport.flush();
    const drainSequence = payload.drainSequence;
    let retryOutcome = flushResult.retryOutcomes.find(
      (outcome) =>
        outcome.sessionId === payload.sessionId &&
        outcome.drainSequence === drainSequence,
    );
    let delivery = retryOutcome?.delivery ?? previousDelivery;
    let acceptedObserverFailed =
      previousObserverFailed ||
      delivery.acceptedObserverFailed === true ||
      flushResult.acceptedObserverFailed;

    const predecessorResolved =
      flushResult.pendingExactRetryCount === 0 &&
      flushResult.retryOutcomes.some(
        (outcome) =>
          outcome.sessionId === payload.sessionId &&
          outcome.drainSequence !== undefined &&
          drainSequence !== undefined &&
          outcome.drainSequence < drainSequence &&
          (outcome.delivery.delivered || outcome.delivery.failure?.retryable === false),
      );
    if (
      !delivery.delivered &&
      previousDelivery.failure?.kind === "blocked" &&
      predecessorResolved
    ) {
      // A terminal drain can be blocked behind an earlier exact retry. Once
      // that predecessor is accepted or permanently rejected, send the terminal
      // bytes again so the server can observe call_ended instead of leaving the
      // call provisional. A rejected predecessor is declared through resync.
      const terminalRetry = await this.transport.send(payload);
      this.reportDeliveryResult(terminalRetry);
      flushResult = await this.transport.flush();
      retryOutcome = flushResult.retryOutcomes.find(
        (outcome) =>
          outcome.sessionId === payload.sessionId &&
          outcome.drainSequence === drainSequence,
      );
      delivery = retryOutcome?.delivery ?? terminalRetry;
      acceptedObserverFailed ||=
        terminalRetry.acceptedObserverFailed === true ||
        delivery.acceptedObserverFailed === true ||
        flushResult.acceptedObserverFailed;
    }

    this.reportDeliveryResult(delivery);
    const result: CaptureSessionFinishResult = {
      payload,
      delivery,
      acceptedObserverFailed,
      pendingExactRetryCount: flushResult.pendingExactRetryCount,
    };
    if (!delivery.delivered || result.pendingExactRetryCount > 0) {
      this.log(
        callEnded
          ? `final capture delivery remains unresolved (pending=${result.pendingExactRetryCount})`
          : `final observation delivery remains unresolved (pending=${result.pendingExactRetryCount})`,
      );
    } else {
      this.log(
        callEnded
          ? "call ended; final capture drain and bounded recovery attempt completed"
          : "capture observation stopped; final drain and bounded recovery attempt completed",
      );
    }
    return result;
  }

  private notifyDrain(payload: CapturePayload): void {
    try {
      this.onDrain?.(payload);
    } catch {
      this.log("capture drain observer failed; delivery continues");
    }
  }

  private reportDeliveryResult(
    result: Awaited<ReturnType<EarshotCaptureTransport["send"]>>,
  ): void {
    if (result.authContextChanged || result.failure?.authContextChanged) {
      this.log(
        "capture was bound to a previous authorization context; it was not cross-sent",
      );
    }
  }
}
