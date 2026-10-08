/**
 * `EarshotCaptureTransport` — the client half of the capture wire.
 *
 * It POSTs drained `CapturePayload`s to the host application's authenticated BFF,
 * which forwards them to Earshot's `POST /v1/capture`, and is responsible for one
 * thing beyond the HTTP call: never letting a delivery failure look like a clean
 * session.
 *
 * Four properties hold by construction:
 *
 * **Versioned.** The payload carries `captureVersion` in its body (see
 * `protocol.ts`), so this client and the server evolve independently of the
 * shared `/v1` route. A server that does not speak the version answers
 * `EARSHOT_UNSUPPORTED_CAPTURE_VERSION`, which is a *permanent* failure here —
 * retrying it would only repeat the same answer.
 *
 * **Cookie/BFF authenticated.** The endpoint and optional CSRF token are host
 * options. This browser transport cannot accept or send project API keys or
 * service JWTs; those credentials stay on the host server.
 *
 * **Bounded.** One delivery is in flight at a time and the pending queue has a
 * hard cap; on overflow the OLDEST payload is dropped. Retries use bounded
 * attempts with exponential backoff. Ambiguous v2 failures retain the exact body
 * until the server resolves that sequence.
 *
 * **Never a silent drop.** A payload this transport gives up on is reported to
 * `onFailure` AND recorded as coverage on the supplied sink (the recorder), so
 * the observations it carried are declared lost in the *next* payload instead of
 * vanishing. The dropped payload's own coverage notes are forwarded too, so the
 * gaps it was already carrying survive the delivery failure. Accepted v2
 * acknowledgements expose only the server call id and finality flag the host
 * needs for its seal flow.
 */

import type { CaptureCoverage, CapturePayload } from "./types.js";

/** The subset of `Response` this transport reads. */
export interface CaptureResponseLike {
  readonly ok: boolean;
  readonly status: number;
  /** Parsed only on success; only the opaque call id and finality flag are exposed. */
  json?(): Promise<unknown>;
}

/** Safe acknowledgement fields from one accepted capture drain. */
export interface CaptureAcknowledgement {
  projectId: string;
  authContextId: string;
  sessionId: string;
  captureVersion: number;
  drainSequence?: number;
  callId?: string;
  finalized?: boolean;
}

/** The subset of `RequestInit` this transport sends. */
export interface CaptureRequestInit {
  method: string;
  headers: Record<string, string>;
  body: string;
  credentials?: string;
  keepalive?: boolean;
}

/** The `fetch` surface (injected in tests; the host `fetch` by default). */
export type FetchLike = (
  url: string,
  init: CaptureRequestInit,
) => Promise<CaptureResponseLike>;

/** Where the coverage for an undelivered payload is ledgered (the recorder). */
export interface CaptureCoverageSink {
  recordCoverage(note: CaptureCoverage): void;
}

/** Why a payload was not delivered. Carries no credential and no payload data. */
export interface CaptureDeliveryFailure {
  /** `http`, `transport`, `queue_overflow`, or a local block behind an unresolved retry. */
  kind: "http" | "transport" | "queue_overflow" | "blocked";
  /** HTTP status when the server answered; omitted for a transport failure. */
  status?: number;
  /** Whether the transport considered this failure worth retrying. */
  retryable: boolean;
  /** How many POST attempts were made for this payload. */
  attempts: number;
  /** Observations (snapshots + device events) the dropped payload carried. */
  droppedObservations: number;
  /** The session the payload belonged to. */
  sessionId: string;
  /** True when the host's effective authorization context no longer matches. */
  authContextChanged?: boolean;
  /** Whether this v2 drain is tracked for a future resync declaration. */
  resyncTracked?: boolean;
  /** Whether the exact body is retained for an idempotent retry. */
  retryRetained?: boolean;
}

/** The outcome of one `send()`. */
export interface CaptureDeliveryResult {
  delivered: boolean;
  attempts: number;
  status?: number;
  /** The effective host scope changed while the server response was pending. */
  authContextChanged?: boolean;
  /** The server accepted the drain, but the host's accepted observer failed. */
  acceptedObserverFailed?: boolean;
  /** Server-minted v2 capture id needed by the host's authorized seal flow. */
  callId?: string;
  /** Present when the server confirms this drain finalized the call. */
  finalized?: boolean;
  failure?: CaptureDeliveryFailure;
}

/** Outcome of one retained exact request attempted during `flush()`. */
export interface CaptureFlushRetryOutcome {
  sessionId: string;
  drainSequence?: number;
  delivery: CaptureDeliveryResult;
}

/** Status reported by a bounded `flush()` retry pass. */
export interface CaptureFlushResult {
  /** At least one accepted observer failed since the previous flush pass. */
  acceptedObserverFailed: boolean;
  /** Exact requests still retained in memory after this bounded pass. */
  pendingExactRetryCount: number;
  /** Delivery result for every retained exact request this pass attempted. */
  retryOutcomes: CaptureFlushRetryOutcome[];
}

export interface CaptureTransportOptions {
  /**
   * The capture endpoint URL — required, with no default. Point it at the host
   * application's authenticated BFF route to Earshot's `POST /v1/capture`.
   * When `csrfToken` is set, this must be a same-origin BFF URL.
   */
  endpoint: string;
  /** The host BFF's cookie CSRF token, sent as `x-earshot-csrf`. */
  csrfToken?: string;
  /** Required project assertion, sent as `x-earshot-project-id`. */
  projectId: string;
  /**
   * Host-supplied opaque scope version. It must change whenever the effective
   * user/project grant changes; raw user ids and credentials do not belong here.
   */
  getAuthContextId: () => string;
  /** `fetch` implementation (default: the host `fetch`). */
  fetch?: FetchLike;
  /** `credentials` mode for the POST (default `same-origin`, so a session cookie flows). */
  credentials?: string;
  /** Max payloads waiting behind the in-flight one (default 8). */
  maxQueuedPayloads?: number;
  /** Max sessions with retained v2 retry/resync state (default 32). */
  maxRecoverySessions?: number;
  /** Max POST attempts per payload, including the first (default 3). */
  maxAttempts?: number;
  /** First retry delay in ms; doubles per attempt (default 500). */
  retryBackoffMs?: number;
  /** Ceiling for the doubling backoff in ms (default 30_000). */
  maxRetryBackoffMs?: number;
  /** Delay function (default `setTimeout`); injected so tests stay deterministic. */
  sleep?: (ms: number) => Promise<void>;
  /** Called for every failed payload. Never given authentication material. */
  onFailure?: (failure: CaptureDeliveryFailure) => void;
  /** Called for accepted drains, including a later retry of an ambiguous request. */
  onAccepted?: (acknowledgement: CaptureAcknowledgement) => void | Promise<void>;
  /** Called when `onAccepted` fails, including on a later flush retry. */
  onAcceptedFailure?: (acknowledgement: CaptureAcknowledgement) => void | Promise<void>;
  /** Where a dropped payload's coverage is ledgered — normally the recorder. */
  coverage?: CaptureCoverageSink;
}

const DEFAULT_MAX_QUEUED_PAYLOADS = 8;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_RETRY_BACKOFF_MS = 500;
const DEFAULT_MAX_RETRY_BACKOFF_MS = 30_000;
const DEFAULT_MAX_RECOVERY_SESSIONS = 32;
const UPLOAD_RESYNC_REASON = "upload_failed_payload_dropped";
const MAX_SCOPE_ASSERTION_LENGTH = 256;

/** Statuses worth another attempt: the same request could succeed later. */
const RETRYABLE_STATUSES = new Set<number>([408, 425, 429, 500, 502, 503, 504]);

interface QueuedPayload {
  payload: CapturePayload;
  body: string;
  resolve: (result: CaptureDeliveryResult) => void;
}

interface PendingResync {
  missedFromSequence: number;
  missedThroughSequence: number;
}

interface PendingExactRetry {
  payload: CapturePayload;
  body: string;
}

function defaultSleep(ms: number): Promise<void> {
  const host = globalThis as unknown as {
    setTimeout?: (handler: () => void, ms: number) => unknown;
  };
  if (typeof host.setTimeout !== "function") return Promise.resolve();
  return new Promise<void>((resolve) => host.setTimeout?.(() => resolve(), ms));
}

function hostFetch(): FetchLike | undefined {
  const host = globalThis as unknown as { fetch?: FetchLike };
  return typeof host.fetch === "function" ? host.fetch.bind(globalThis) : undefined;
}

function serializePayload(payload: CapturePayload): string {
  const body = JSON.stringify(payload);
  if (typeof body !== "string") {
    throw new TypeError("capture payload did not serialize to a JSON object");
  }
  return body;
}

function isValidScopeAssertion(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_SCOPE_ASSERTION_LENGTH &&
    value === value.trim() &&
    !/[\r\n]/.test(value)
  );
}

function snapshotPayload(payload: CapturePayload): {
  payload: CapturePayload;
  body: string;
} {
  const body = serializePayload(payload);
  const parsed: unknown = JSON.parse(body);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError("capture payload did not serialize to a JSON object");
  }
  return { payload: parsed as CapturePayload, body };
}

function positiveOption(
  value: number | undefined,
  fallback: number,
  label: string,
): number {
  if (value === undefined) return fallback;
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value <= 0 ||
    Math.floor(value) < 1
  ) {
    throw new RangeError(
      `${label} must be a positive finite number (got ${String(value)})`,
    );
  }
  return Math.floor(value);
}

export class EarshotCaptureTransport {
  private readonly endpoint: string;
  private readonly csrfToken?: string;
  private readonly projectId: string;
  private readonly getAuthContextId: () => string;
  private readonly authContextId: string;
  private readonly fetchImpl: FetchLike;
  private readonly credentials: string;
  private readonly maxQueuedPayloads: number;
  private readonly maxRecoverySessions: number;
  private readonly maxAttempts: number;
  private readonly retryBackoffMs: number;
  private readonly maxRetryBackoffMs: number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly onFailure?: (failure: CaptureDeliveryFailure) => void;
  private readonly onAccepted?: (acknowledgement: CaptureAcknowledgement) => void;
  private readonly onAcceptedFailure?: (
    acknowledgement: CaptureAcknowledgement,
  ) => void | Promise<void>;
  private readonly coverage?: CaptureCoverageSink;

  private readonly queue: QueuedPayload[] = [];
  private readonly pendingResync = new Map<string, PendingResync>();
  private readonly pendingExactRetry = new Map<string, PendingExactRetry>();
  private recoverySaturated = false;
  private draining: Promise<void> | null = null;
  private stopped = false;
  private acceptedObserverFailedSinceFlush = false;

  constructor(options: CaptureTransportOptions) {
    if (typeof options?.endpoint !== "string" || options.endpoint.length === 0) {
      throw new TypeError("createCaptureTransport: endpoint is required");
    }
    if (!isValidScopeAssertion(options.projectId)) {
      throw new TypeError("createCaptureTransport: projectId is required");
    }
    if (typeof options.getAuthContextId !== "function") {
      throw new TypeError("createCaptureTransport: getAuthContextId is required");
    }
    const fetchImpl = options.fetch ?? hostFetch();
    if (!fetchImpl) {
      throw new TypeError("createCaptureTransport: no fetch implementation available");
    }
    let authContextId: string;
    try {
      authContextId = options.getAuthContextId();
    } catch {
      throw new TypeError("createCaptureTransport: auth context is unavailable");
    }
    if (!isValidScopeAssertion(authContextId)) {
      throw new TypeError("createCaptureTransport: auth context is invalid");
    }
    this.endpoint = options.endpoint;
    this.csrfToken = options.csrfToken;
    this.projectId = options.projectId;
    this.getAuthContextId = options.getAuthContextId;
    this.authContextId = authContextId;
    this.fetchImpl = fetchImpl;
    this.credentials = options.credentials ?? "same-origin";
    this.maxQueuedPayloads = positiveOption(
      options.maxQueuedPayloads,
      DEFAULT_MAX_QUEUED_PAYLOADS,
      "maxQueuedPayloads",
    );
    this.maxRecoverySessions = positiveOption(
      options.maxRecoverySessions,
      DEFAULT_MAX_RECOVERY_SESSIONS,
      "maxRecoverySessions",
    );
    this.maxAttempts = positiveOption(
      options.maxAttempts,
      DEFAULT_MAX_ATTEMPTS,
      "maxAttempts",
    );
    this.retryBackoffMs = positiveOption(
      options.retryBackoffMs,
      DEFAULT_RETRY_BACKOFF_MS,
      "retryBackoffMs",
    );
    this.maxRetryBackoffMs = positiveOption(
      options.maxRetryBackoffMs,
      DEFAULT_MAX_RETRY_BACKOFF_MS,
      "maxRetryBackoffMs",
    );
    this.sleep = options.sleep ?? defaultSleep;
    this.onFailure = options.onFailure;
    this.onAccepted = options.onAccepted;
    this.onAcceptedFailure = options.onAcceptedFailure;
    this.coverage = options.coverage;
  }

  /** Payloads waiting to be posted (excludes the one in flight). */
  get queuedCount(): number {
    return this.queue.length;
  }

  /**
   * Queue one payload and resolve when it is finally delivered or given up on.
   *
   * Deliveries run one at a time and in order, so retries cannot reorder a
   * session's batches. If the queue is already full the OLDEST waiting payload
   * is dropped (its `send()` resolves with the failure and its observations are
   * recorded as coverage) — the newest evidence is the evidence worth keeping.
   */
  send(payload: CapturePayload): Promise<CaptureDeliveryResult> {
    if (this.stopped) {
      return Promise.resolve(
        this.giveUp(payload, { kind: "transport", retryable: false }, 0),
      );
    }
    if (!this.isAuthContextCurrent()) {
      return Promise.resolve(
        this.giveUp(
          payload,
          { kind: "blocked", retryable: false, authContextChanged: true },
          0,
          undefined,
          false,
        ),
      );
    }
    if (
      this.recoverySaturated &&
      this.drainSequence(payload) !== undefined &&
      !this.hasRecoveryState(payload.sessionId)
    ) {
      return Promise.resolve(
        this.giveUp(payload, { kind: "blocked", retryable: false }, 0),
      );
    }
    let request: ReturnType<typeof snapshotPayload>;
    try {
      request = snapshotPayload(payload);
    } catch {
      return Promise.resolve(
        this.giveUp(payload, { kind: "transport", retryable: false }, 0),
      );
    }
    return new Promise<CaptureDeliveryResult>((resolve) => {
      while (this.queue.length >= this.maxQueuedPayloads) {
        const evicted = this.queue.shift();
        if (!evicted) break;
        evicted.resolve(
          this.giveUp(evicted.payload, { kind: "queue_overflow", retryable: false }, 0),
        );
      }
      this.queue.push({ ...request, resolve });
      void this.drain();
    });
  }

  /**
   * Resolve once queued/in-flight work settles. Also retries retained exact v2
   * requests once, including after `stop()` has closed admission.
   */
  async flush(): Promise<CaptureFlushResult> {
    const retryOutcomes: CaptureFlushRetryOutcome[] = [];
    for (;;) {
      if (this.draining) {
        await this.draining;
        continue;
      }
      if (this.pendingExactRetry.size === 0) break;
      await this.drain(true, retryOutcomes);
      break;
    }
    const result = {
      acceptedObserverFailed: this.acceptedObserverFailedSinceFlush,
      pendingExactRetryCount: this.pendingExactRetry.size,
      retryOutcomes,
    };
    this.acceptedObserverFailedSinceFlush = false;
    return result;
  }

  /**
   * Stop accepting work and give up on everything still queued — recording each
   * abandoned payload as coverage rather than discarding it quietly.
   */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    for (const queued of this.queue.splice(0)) {
      queued.resolve(
        this.giveUp(queued.payload, { kind: "transport", retryable: false }, 0),
      );
    }
  }

  // -- internals -------------------------------------------------------------

  private drain(
    retryIdleRequests = false,
    retryOutcomes?: CaptureFlushRetryOutcome[],
  ): Promise<void> {
    if (this.draining) return this.draining;
    const run = (async () => {
      try {
        for (;;) {
          const next = this.queue.shift();
          if (!next) break;
          if (!this.isAuthContextCurrent()) {
            next.resolve(
              this.giveUp(
                next.payload,
                { kind: "blocked", retryable: false, authContextChanged: true },
                0,
              ),
            );
            continue;
          }
          if (
            this.recoverySaturated &&
            this.drainSequence(next.payload) !== undefined &&
            !this.hasRecoveryState(next.payload.sessionId)
          ) {
            next.resolve(
              this.giveUp(next.payload, { kind: "blocked", retryable: false }, 0),
            );
            continue;
          }
          const pendingRetry = this.pendingExactRetry.get(next.payload.sessionId);
          if (pendingRetry) {
            const retryResult = await this.deliver(
              pendingRetry.payload,
              pendingRetry.body,
            );
            retryOutcomes?.push({
              sessionId: next.payload.sessionId,
              drainSequence: this.drainSequence(pendingRetry.payload),
              delivery: retryResult,
            });
            if (retryResult.delivered) {
              this.pendingExactRetry.delete(next.payload.sessionId);
              this.clearResync(pendingRetry.payload);
            } else if (
              this.pendingExactRetry.has(next.payload.sessionId) ||
              this.recoverySaturated
            ) {
              const authContextChanged = !this.isAuthContextCurrent();
              next.resolve(
                this.giveUp(
                  next.payload,
                  {
                    kind: "blocked",
                    retryable: false,
                    ...(authContextChanged ? { authContextChanged: true } : {}),
                  },
                  0,
                ),
              );
              continue;
            }
            // A permanent rejection clears the exact-retry state and records the
            // old sequence as a real loss. The current drain can then declare it.
          }
          if (
            this.recoverySaturated &&
            this.drainSequence(next.payload) !== undefined &&
            !this.hasRecoveryState(next.payload.sessionId)
          ) {
            next.resolve(
              this.giveUp(next.payload, { kind: "blocked", retryable: false }, 0),
            );
            continue;
          }
          const payload = this.prepareResync(next.payload);
          const body = payload === next.payload ? next.body : serializePayload(payload);
          const result = await this.deliver(payload, body);
          if (result.delivered) this.clearResync(payload);
          next.resolve(result);
        }
        if (retryIdleRequests) {
          for (const [sessionId, pendingRetry] of [...this.pendingExactRetry]) {
            const result = await this.deliver(pendingRetry.payload, pendingRetry.body);
            retryOutcomes?.push({
              sessionId,
              drainSequence: this.drainSequence(pendingRetry.payload),
              delivery: result,
            });
            if (result.delivered) {
              this.pendingExactRetry.delete(sessionId);
              this.clearResync(pendingRetry.payload);
            }
          }
        }
      } finally {
        this.draining = null;
      }
    })();
    this.draining = run;
    return run;
  }

  private async deliver(
    payload: CapturePayload,
    exactBody?: string,
  ): Promise<CaptureDeliveryResult> {
    const body = exactBody ?? serializePayload(payload);
    let attempts = 0;
    let lastFailure: { kind: "http" | "transport"; status?: number; retryable: boolean } =
      {
        kind: "transport",
        retryable: true,
      };
    while (attempts < this.maxAttempts) {
      if (!this.isAuthContextCurrent()) {
        return this.blockForAuthContextChange(payload, body, attempts);
      }
      attempts += 1;
      let response: CaptureResponseLike;
      try {
        response = await this.post(payload, body);
      } catch {
        // The error is deliberately not inspected or surfaced: a fetch rejection
        // can carry the request body or headers in its message.
        if (!this.isAuthContextCurrent()) {
          return this.blockForAuthContextChange(payload, body, attempts);
        }
        lastFailure = { kind: "transport", retryable: true };
        if (attempts >= this.maxAttempts) break;
        await this.sleep(this.backoffFor(attempts));
        continue;
      }
      if (!this.isAuthContextCurrent()) {
        return this.blockForAuthContextChange(payload, body, attempts);
      }
      if (response.ok) {
        const acknowledgement = await this.readAcknowledgement(payload, response);
        if (!this.isAuthContextCurrent()) {
          return this.blockForAuthContextChange(payload, body, attempts);
        }
        const acceptedObserverFailed = !(await this.notifyAccepted(acknowledgement));
        if (acceptedObserverFailed) {
          this.notifyAcceptedFailure(acknowledgement);
        }
        const authContextChanged = !this.isAuthContextCurrent();
        const result: CaptureDeliveryResult = {
          delivered: true,
          attempts,
          status: response.status,
          ...(acceptedObserverFailed ? { acceptedObserverFailed: true } : {}),
          ...(authContextChanged ? { authContextChanged: true } : {}),
        };
        if (!authContextChanged) {
          if (acknowledgement.callId !== undefined) {
            result.callId = acknowledgement.callId;
          }
          if (acknowledgement.finalized !== undefined) {
            result.finalized = acknowledgement.finalized;
          }
        }
        return result;
      }
      const retryable = RETRYABLE_STATUSES.has(response.status);
      lastFailure = { kind: "http", status: response.status, retryable };
      // A rejected payload (bad version, too large, unauthorized) will be
      // rejected identically forever; retrying only delays the honest answer.
      if (!retryable || attempts >= this.maxAttempts) break;
      await this.sleep(this.backoffFor(attempts));
    }
    if (lastFailure.retryable && this.drainSequence(payload) !== undefined) {
      return this.retainExactRetry(payload, body, lastFailure, attempts);
    }
    const pendingRetry = this.pendingExactRetry.get(payload.sessionId);
    const wasExactRetry = pendingRetry?.payload.drainSequence === payload.drainSequence;
    const result = this.giveUp(payload, lastFailure, attempts);
    if (wasExactRetry) this.pendingExactRetry.delete(payload.sessionId);
    return result;
  }

  private post(payload: CapturePayload, body: string): Promise<CaptureResponseLike> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    // Hosted service credentials are never available to browser code.
    if (this.csrfToken) headers["x-earshot-csrf"] = this.csrfToken;
    headers["x-earshot-project-id"] = this.projectId;
    headers["x-earshot-auth-context-id"] = this.authContextId;
    if (payload.traceContext?.traceparent) {
      headers.traceparent = payload.traceContext.traceparent;
    }
    return this.fetchImpl(this.endpoint, {
      method: "POST",
      headers,
      body,
      credentials: this.credentials,
    });
  }

  private backoffFor(attempt: number): number {
    const delay = this.retryBackoffMs * 2 ** (attempt - 1);
    return Math.min(delay, this.maxRetryBackoffMs);
  }

  private async readAcknowledgement(
    payload: CapturePayload,
    response: CaptureResponseLike,
  ): Promise<CaptureAcknowledgement> {
    const sequence = this.drainSequence(payload);
    const acknowledgement: CaptureAcknowledgement = {
      projectId: this.projectId,
      authContextId: this.authContextId,
      sessionId: payload.sessionId,
      captureVersion: payload.captureVersion,
      ...(sequence === undefined ? {} : { drainSequence: sequence }),
    };
    if (!response.json) return acknowledgement;
    try {
      const body = await response.json();
      if (body === null || typeof body !== "object") return acknowledgement;
      const responseBody = body as Record<string, unknown>;
      const callId = responseBody.call_id;
      if (typeof callId === "string" && callId.length > 0 && callId.length <= 256) {
        acknowledgement.callId = callId;
      }
      if (typeof responseBody.finalized === "boolean") {
        acknowledgement.finalized = responseBody.finalized;
      }
      return acknowledgement;
    } catch {
      // A malformed success body does not undo the HTTP acceptance. The host can
      // still recover the live session through its authenticated list endpoint.
      return acknowledgement;
    }
  }

  private retainExactRetry(
    payload: CapturePayload,
    body: string,
    failure: {
      kind: "http" | "transport" | "blocked";
      status?: number;
      retryable: boolean;
      authContextChanged?: boolean;
    },
    attempts: number,
  ): CaptureDeliveryResult {
    const retryRetained = this.rememberExactRetry(payload, body);
    if (!retryRetained) {
      return this.giveUp(
        payload,
        {
          kind: failure.kind,
          status: failure.status,
          retryable: false,
          ...(failure.authContextChanged ? { authContextChanged: true } : {}),
        },
        attempts,
        false,
      );
    }
    const reported: CaptureDeliveryFailure = {
      kind: failure.kind,
      retryable: true,
      attempts,
      droppedObservations:
        (payload.snapshots?.length ?? 0) + (payload.deviceEvents?.length ?? 0),
      sessionId: payload.sessionId,
      retryRetained,
      ...(failure.authContextChanged ? { authContextChanged: true } : {}),
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
    this.notifyFailure(reported);
    return {
      delivered: false,
      attempts,
      failure: reported,
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
  }

  /** Declare preceding transport losses on this v2 drain without mutating caller data. */
  private prepareResync(payload: CapturePayload): CapturePayload {
    const sequence = this.drainSequence(payload);
    if (sequence === undefined) return payload;
    const pending = this.pendingResync.get(payload.sessionId);
    if (!pending) return payload;
    const missedThroughSequence = Math.min(pending.missedThroughSequence, sequence - 1);
    if (pending.missedFromSequence > missedThroughSequence) return payload;
    return {
      ...payload,
      resync: {
        missedFromSequence: pending.missedFromSequence,
        // Any sequence skipped before this drain is absent from the transport,
        // whether its payload was never sent or was abandoned here.
        missedThroughSequence,
        reason: UPLOAD_RESYNC_REASON,
      },
    };
  }

  private clearResync(payload: CapturePayload): void {
    const sequence = this.drainSequence(payload);
    if (sequence === undefined) return;
    const pending = this.pendingResync.get(payload.sessionId);
    if (!pending || sequence < pending.missedFromSequence) return;
    if (sequence >= pending.missedThroughSequence) {
      this.pendingResync.delete(payload.sessionId);
      return;
    }
    this.pendingResync.set(payload.sessionId, {
      missedFromSequence: sequence + 1,
      missedThroughSequence: pending.missedThroughSequence,
    });
  }

  /** Remember a failed v2 sequence; the range is bounded by session count. */
  private rememberDroppedDrain(payload: CapturePayload): boolean | undefined {
    const sequence = this.drainSequence(payload);
    if (sequence === undefined) return undefined;
    const existing = this.pendingResync.get(payload.sessionId);
    if (
      this.recoverySaturated &&
      !existing &&
      !this.pendingExactRetry.has(payload.sessionId)
    ) {
      return false;
    }
    if (
      !existing &&
      !this.pendingExactRetry.has(payload.sessionId) &&
      this.recoverySessionCount() >= this.maxRecoverySessions
    ) {
      this.recoverySaturated = true;
      return false;
    }
    const declared = payload.resync;
    const missedFromSequence = Math.min(
      existing?.missedFromSequence ?? sequence,
      declared?.missedFromSequence ?? sequence,
    );
    this.pendingResync.set(payload.sessionId, {
      missedFromSequence,
      missedThroughSequence: Math.max(
        existing?.missedThroughSequence ?? sequence,
        sequence,
      ),
    });
    return true;
  }

  private rememberExactRetry(payload: CapturePayload, body: string): boolean {
    const existing = this.pendingExactRetry.get(payload.sessionId);
    if (existing) return existing.body === body;
    if (this.recoverySaturated && !this.pendingResync.has(payload.sessionId)) {
      return false;
    }
    if (
      !this.pendingResync.has(payload.sessionId) &&
      this.recoverySessionCount() >= this.maxRecoverySessions
    ) {
      this.recoverySaturated = true;
      return false;
    }
    this.pendingExactRetry.set(payload.sessionId, { payload, body });
    return true;
  }

  private recoverySessionCount(): number {
    const sessions = new Set(this.pendingResync.keys());
    for (const sessionId of this.pendingExactRetry.keys()) sessions.add(sessionId);
    return sessions.size;
  }

  /** Whether an already retained session can still be recovered after saturation. */
  private hasRecoveryState(sessionId: string): boolean {
    return this.pendingResync.has(sessionId) || this.pendingExactRetry.has(sessionId);
  }

  private drainSequence(payload: CapturePayload): number | undefined {
    if (
      payload.captureVersion !== 2 ||
      !Number.isSafeInteger(payload.drainSequence) ||
      (payload.drainSequence ?? 0) < 1
    ) {
      return undefined;
    }
    return payload.drainSequence;
  }

  /**
   * Give up on a payload — and say so. The lost observations become a coverage
   * note on the sink, and the notes the payload was already carrying are
   * forwarded so they are not lost along with it.
   */
  private giveUp(
    payload: CapturePayload,
    failure: {
      kind: "http" | "transport" | "queue_overflow" | "blocked";
      status?: number;
      retryable: boolean;
      authContextChanged?: boolean;
    },
    attempts: number,
    retryRetained?: boolean,
    trackRecovery = true,
  ): CaptureDeliveryResult {
    const resyncTracked = trackRecovery ? this.rememberDroppedDrain(payload) : undefined;
    const droppedObservations =
      (payload.snapshots?.length ?? 0) + (payload.deviceEvents?.length ?? 0);
    const reported: CaptureDeliveryFailure = {
      kind: failure.kind,
      retryable: failure.retryable,
      attempts,
      droppedObservations,
      sessionId: payload.sessionId,
      ...(retryRetained === undefined ? {} : { retryRetained }),
      ...(resyncTracked === undefined ? {} : { resyncTracked }),
      ...(failure.authContextChanged ? { authContextChanged: true } : {}),
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
    this.recordCoverage({
      signal: "capture.upload",
      availability: "partial",
      reason:
        failure.kind === "queue_overflow"
          ? "upload_queue_overflow_oldest_dropped"
          : "upload_failed_payload_dropped",
      droppedCount: droppedObservations,
    });
    for (const note of payload.coverage ?? []) {
      this.recordCoverage(note);
    }
    this.notifyFailure(reported);
    return {
      delivered: false,
      attempts,
      failure: reported,
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
  }

  private recordCoverage(note: CaptureCoverage): void {
    try {
      this.coverage?.recordCoverage(note);
    } catch {
      // Observer failures cannot strand delivery or change its result.
    }
  }

  private notifyFailure(failure: CaptureDeliveryFailure): void {
    try {
      this.onFailure?.(failure);
    } catch {
      // Metrics/reporting failures cannot strand delivery or change its result.
    }
  }

  private async notifyAccepted(
    acknowledgement: CaptureAcknowledgement,
  ): Promise<boolean> {
    try {
      await this.onAccepted?.(acknowledgement);
      return true;
    } catch {
      // Host callback errors cannot undo an accepted server drain.
      this.acceptedObserverFailedSinceFlush = true;
      return false;
    }
  }

  private notifyAcceptedFailure(acknowledgement: CaptureAcknowledgement): void {
    try {
      const notification = this.onAcceptedFailure?.(acknowledgement);
      if (notification) {
        void Promise.resolve(notification).catch(() => {
          // Error-reporting failures cannot affect the accepted server response.
        });
      }
    } catch {
      // Failure-reporting callbacks cannot change an accepted server response.
    }
  }

  private isAuthContextCurrent(): boolean {
    try {
      const current = this.getAuthContextId();
      return isValidScopeAssertion(current) && current === this.authContextId;
    } catch {
      return false;
    }
  }

  private blockForAuthContextChange(
    payload: CapturePayload,
    body: string,
    attempts: number,
  ): CaptureDeliveryResult {
    if (this.drainSequence(payload) !== undefined) {
      return this.retainExactRetry(
        payload,
        body,
        { kind: "blocked", retryable: true, authContextChanged: true },
        attempts,
      );
    }
    return this.giveUp(
      payload,
      { kind: "blocked", retryable: false, authContextChanged: true },
      attempts,
    );
  }
}

/** Functional constructor mirroring the class (parity with the recorder). */
export function createCaptureTransport(
  options: CaptureTransportOptions,
): EarshotCaptureTransport {
  return new EarshotCaptureTransport(options);
}
