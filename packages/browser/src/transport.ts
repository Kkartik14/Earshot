/** POST drained payloads to `/v1/capture` without hiding delivery loss.
 * Credentials are request options and never enter errors or failure records.
 * The queue and retry count are bounded; only transient failures are retried.
 * Undelivered observations and their existing coverage are recorded on the next
 * payload, and each failure is reported through `onFailure`.
 */

import type { CaptureCoverage, CapturePayload } from "./types.js";

/** The subset of `Response` this transport reads. */
export interface CaptureResponseLike {
  readonly ok: boolean;
  readonly status: number;
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
  /** `http` (the server answered an error), `transport` (the call threw), or `queue_overflow`. */
  kind: "http" | "transport" | "queue_overflow";
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
}

/** The outcome of one `send()`. */
export interface CaptureDeliveryResult {
  delivered: boolean;
  attempts: number;
  status?: number;
  failure?: CaptureDeliveryFailure;
}

export interface CaptureTransportOptions {
  /**
   * The capture endpoint URL — required, with no default. Point it at the
   * earshot backend's `POST /v1/capture` (absolute, or same-origin relative).
   */
  endpoint: string;
  /**
   * A project API key, sent as `Authorization: Bearer …`. Omit it when the page
   * authenticates with the viewer session cookie instead (then supply
   * `csrfToken`, which that cookie's CSRF protection requires on POST).
   */
  apiKey?: string;
  /** The viewer session's CSRF token, sent as `x-earshot-csrf`. */
  csrfToken?: string;
  /** Optional project assertion, sent as `x-earshot-project-id`. */
  projectId?: string;
  /** `fetch` implementation (default: the host `fetch`). */
  fetch?: FetchLike;
  /** `credentials` mode for the POST (default `same-origin`, so a session cookie flows). */
  credentials?: string;
  /** Max payloads waiting behind the in-flight one (default 8). */
  maxQueuedPayloads?: number;
  /** Max POST attempts per payload, including the first (default 3). */
  maxAttempts?: number;
  /** First retry delay in ms; doubles per attempt (default 500). */
  retryBackoffMs?: number;
  /** Ceiling for the doubling backoff in ms (default 30_000). */
  maxRetryBackoffMs?: number;
  /** Delay function (default `setTimeout`); injected so tests stay deterministic. */
  sleep?: (ms: number) => Promise<void>;
  /** Called for every payload this transport gives up on. Never given the credential. */
  onFailure?: (failure: CaptureDeliveryFailure) => void;
  /** Where a dropped payload's coverage is ledgered — normally the recorder. */
  coverage?: CaptureCoverageSink;
}

const DEFAULT_MAX_QUEUED_PAYLOADS = 8;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_RETRY_BACKOFF_MS = 500;
const DEFAULT_MAX_RETRY_BACKOFF_MS = 30_000;

/** Statuses worth another attempt: the same request could succeed later. */
const RETRYABLE_STATUSES = new Set<number>([408, 425, 429, 500, 502, 503, 504]);

interface QueuedPayload {
  payload: CapturePayload;
  resolve: (result: CaptureDeliveryResult) => void;
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

function positiveOption(
  value: number | undefined,
  fallback: number,
  label: string,
): number {
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new RangeError(
      `${label} must be a positive finite number (got ${String(value)})`,
    );
  }
  return Math.floor(value);
}

export class EarshotCaptureTransport {
  private readonly endpoint: string;
  private readonly apiKey?: string;
  private readonly csrfToken?: string;
  private readonly projectId?: string;
  private readonly fetchImpl: FetchLike;
  private readonly credentials: string;
  private readonly maxQueuedPayloads: number;
  private readonly maxAttempts: number;
  private readonly retryBackoffMs: number;
  private readonly maxRetryBackoffMs: number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly onFailure?: (failure: CaptureDeliveryFailure) => void;
  private readonly coverage?: CaptureCoverageSink;

  private readonly queue: QueuedPayload[] = [];
  private draining: Promise<void> | null = null;
  private stopped = false;

  constructor(options: CaptureTransportOptions) {
    if (typeof options?.endpoint !== "string" || options.endpoint.length === 0) {
      throw new TypeError("createCaptureTransport: endpoint is required");
    }
    const fetchImpl = options.fetch ?? hostFetch();
    if (!fetchImpl) {
      throw new TypeError("createCaptureTransport: no fetch implementation available");
    }
    this.endpoint = options.endpoint;
    this.apiKey = options.apiKey;
    this.csrfToken = options.csrfToken;
    this.projectId = options.projectId;
    this.fetchImpl = fetchImpl;
    this.credentials = options.credentials ?? "same-origin";
    this.maxQueuedPayloads = positiveOption(
      options.maxQueuedPayloads,
      DEFAULT_MAX_QUEUED_PAYLOADS,
      "maxQueuedPayloads",
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
    return new Promise<CaptureDeliveryResult>((resolve) => {
      while (this.queue.length >= this.maxQueuedPayloads) {
        const evicted = this.queue.shift();
        if (!evicted) break;
        evicted.resolve(
          this.giveUp(evicted.payload, { kind: "queue_overflow", retryable: false }, 0),
        );
      }
      this.queue.push({ payload, resolve });
      void this.drain();
    });
  }

  /** Resolve once the queue (and the delivery in flight) has settled. */
  async flush(): Promise<void> {
    while (this.draining) await this.draining;
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


  private drain(): Promise<void> {
    if (this.draining) return this.draining;
    const run = (async () => {
      try {
        for (;;) {
          const next = this.queue.shift();
          if (!next) break;
          next.resolve(await this.deliver(next.payload));
        }
      } finally {
        this.draining = null;
      }
    })();
    this.draining = run;
    return run;
  }

  private async deliver(payload: CapturePayload): Promise<CaptureDeliveryResult> {
    let attempts = 0;
    let lastFailure: { kind: "http" | "transport"; status?: number; retryable: boolean } =
      {
        kind: "transport",
        retryable: true,
      };
    while (attempts < this.maxAttempts) {
      attempts += 1;
      let response: CaptureResponseLike;
      try {
        response = await this.post(payload);
      } catch {
        // The error is deliberately not inspected or surfaced: a fetch rejection
        // can carry the request (and therefore the credential) in its message.
        lastFailure = { kind: "transport", retryable: true };
        if (attempts >= this.maxAttempts || this.stopped) break;
        await this.sleep(this.backoffFor(attempts));
        continue;
      }
      if (response.ok) return { delivered: true, attempts, status: response.status };
      const retryable = RETRYABLE_STATUSES.has(response.status);
      lastFailure = { kind: "http", status: response.status, retryable };
      // A rejected payload (bad version, too large, unauthorized) will be
      // rejected identically forever; retrying only delays the honest answer.
      if (!retryable || attempts >= this.maxAttempts || this.stopped) break;
      await this.sleep(this.backoffFor(attempts));
    }
    return this.giveUp(payload, lastFailure, attempts);
  }

  private post(payload: CapturePayload): Promise<CaptureResponseLike> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    // The credential is written here and nowhere else in this module.
    if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;
    if (this.csrfToken) headers["x-earshot-csrf"] = this.csrfToken;
    if (this.projectId) headers["x-earshot-project-id"] = this.projectId;
    if (payload.traceContext?.traceparent) {
      headers.traceparent = payload.traceContext.traceparent;
    }
    return this.fetchImpl(this.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      credentials: this.credentials,
    });
  }

  private backoffFor(attempt: number): number {
    const delay = this.retryBackoffMs * 2 ** (attempt - 1);
    return Math.min(delay, this.maxRetryBackoffMs);
  }

  /**
   * Give up on a payload — and say so. The lost observations become a coverage
   * note on the sink, and the notes the payload was already carrying are
   * forwarded so they are not lost along with it.
   */
  private giveUp(
    payload: CapturePayload,
    failure: {
      kind: "http" | "transport" | "queue_overflow";
      status?: number;
      retryable: boolean;
    },
    attempts: number,
  ): CaptureDeliveryResult {
    const droppedObservations =
      (payload.snapshots?.length ?? 0) + (payload.deviceEvents?.length ?? 0);
    const reported: CaptureDeliveryFailure = {
      kind: failure.kind,
      retryable: failure.retryable,
      attempts,
      droppedObservations,
      sessionId: payload.sessionId,
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
    if (this.coverage) {
      this.coverage.recordCoverage({
        signal: "capture.upload",
        availability: "partial",
        reason:
          failure.kind === "queue_overflow"
            ? "upload_queue_overflow_oldest_dropped"
            : "upload_failed_payload_dropped",
        droppedCount: droppedObservations,
      });
      for (const note of payload.coverage ?? []) {
        this.coverage.recordCoverage(note);
      }
    }
    this.onFailure?.(reported);
    return {
      delivered: false,
      attempts,
      failure: reported,
      ...(failure.status === undefined ? {} : { status: failure.status }),
    };
  }
}

/** Functional constructor mirroring the class (parity with the recorder). */
export function createCaptureTransport(
  options: CaptureTransportOptions,
): EarshotCaptureTransport {
  return new EarshotCaptureTransport(options);
}
