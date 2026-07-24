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
  CaptureDeliveryFailure,
  CapturePayload,
  EarshotBrowserRecorder,
  EarshotCaptureTransport,
} from "@earshot/browser";

/** Where and how to authenticate the `POST /v1/capture` call. */
export interface CaptureEndpointConfig {
  /** Absolute or same-origin-relative capture endpoint. No default. */
  endpoint: string;
  /** Project API key (Bearer). Omit to use the viewer session cookie + csrfToken. */
  apiKey?: string;
  /** Viewer-session CSRF token, required for a cookie-authenticated POST. */
  csrfToken?: string;
  /** Optional project assertion header. */
  projectId?: string;
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
}

const DEFAULT_DRAIN_INTERVAL_MS = 10_000;

export class CaptureSession {
  readonly recorder: EarshotBrowserRecorder;
  private readonly transport: EarshotCaptureTransport;
  private readonly log: (message: string) => void;
  private readonly onDrain?: (payload: CapturePayload) => void;
  private readonly drainIntervalMs: number;
  private timer: ReturnType<typeof setInterval> | undefined;

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
      apiKey: options.endpoint.apiKey,
      csrfToken: options.endpoint.csrfToken,
      projectId: options.endpoint.projectId,
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
    if (this.timer !== undefined) return;
    this.timer = setInterval(() => {
      void this.drainAndSend();
    }, this.drainIntervalMs);
    this.log(`draining + POSTing every ${this.drainIntervalMs}ms`);
  }

  private async drainAndSend(): Promise<void> {
    const payload = this.recorder.drain();
    this.onDrain?.(payload);
    const result = await this.transport.send(payload);
    if (result.delivered) {
      this.log(
        `batch delivered (status=${result.status}, ` +
          `drainSequence=${payload.drainSequence ?? "n/a"}, ` +
          `snapshots=${payload.snapshots.length}, events=${payload.deviceEvents.length})`,
      );
    }
  }

  /**
   * Stop sampling, drain and POST the final batch, wait for the queue to settle,
   * then release the recorder and transport. Idempotent-friendly for a page
   * unload / stop button.
   */
  async stop(): Promise<CapturePayload> {
    if (this.timer !== undefined) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    this.recorder.stop();
    const finalBatch = this.recorder.drain();
    this.onDrain?.(finalBatch);
    await this.transport.send(finalBatch);
    await this.transport.flush();
    this.transport.stop();
    this.log("session stopped; final batch flushed");
    return finalBatch;
  }
}
