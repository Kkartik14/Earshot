import {
  parseTraceBundle,
  type Providers,
  type Session,
  type Span,
  type TraceBundle,
  type TraceError,
  type Turn,
} from "@earshot/schema";

/**
 * Turns a validated {@link TraceBundle} into a display-ready call model:
 * per-turn waterfalls plus derived latency and failure metrics.
 *
 * Everything here is DERIVED. The SDK emits raw facts (span timings, well-known
 * attribute values like `firstTokenMs`); this module computes TTFT, response
 * latency, tool time, interruption rate, and percentiles from them.
 */

export interface WaterfallSpan {
  readonly spanId: string;
  readonly type: Span["type"];
  readonly name?: string;
  readonly provider?: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly durationMs: number;
  /** Offset from the start of the turn — the x-position of a waterfall row. */
  readonly offsetMs: number;
  readonly status: Span["status"];
  readonly attributes: Record<string, unknown>;
  readonly error: TraceError | null;
}

export interface TurnMetrics {
  /** End-of-utterance -> first LLM token. */
  readonly ttftMs?: number;
  /** TTS time-to-first-byte (provider-reported). */
  readonly ttfbMs?: number;
  /** End-of-utterance -> first audible output (what the caller waits through). */
  readonly responseMs?: number;
  /** End-of-utterance detection cost. */
  readonly endpointMs?: number;
  /** Total time spent in tool calls. */
  readonly toolMs: number;
}

export interface TurnAnalysis {
  readonly turnId: string;
  readonly index: number;
  readonly status: Turn["status"];
  readonly userTranscript?: string;
  readonly agentText?: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly durationMs: number;
  readonly spans: readonly WaterfallSpan[];
  readonly metrics: TurnMetrics;
  readonly interruption: Turn["interruption"];
  readonly error: TraceError | null;
}

export interface CallSummary {
  readonly turnCount: number;
  readonly completedTurns: number;
  readonly interruptedTurns: number;
  readonly failedTurns: number;
  readonly noResponseTurns: number;
  readonly interruptionRate: number;
  readonly p50ResponseMs?: number;
  readonly p95ResponseMs?: number;
  readonly avgTtftMs?: number;
}

export interface CallAnalysis {
  readonly sessionId: string;
  readonly status: Session["status"];
  readonly agent: Session["agent"];
  readonly providers: Providers;
  readonly startedAt: string;
  readonly endedAt?: string;
  readonly durationMs?: number;
  readonly turns: readonly TurnAnalysis[];
  readonly summary: CallSummary;
}

function round(n: number): number {
  return Math.round(n);
}

function numAttr(attributes: Record<string, unknown>, key: string): number | undefined {
  const v = attributes[key];
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

/** Nearest-rank percentile — deterministic and dependency-free. */
function percentile(values: readonly number[], p: number): number | undefined {
  if (values.length === 0) {
    return undefined;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.ceil((p / 100) * sorted.length);
  const idx = Math.min(sorted.length - 1, Math.max(0, rank - 1));
  return sorted[idx];
}

function analyzeTurn(turn: Turn, spans: readonly Span[]): TurnAnalysis {
  const ordered = [...spans].sort((a, b) => a.startMs - b.startMs);

  const spanStart = ordered.length > 0 ? Math.min(...ordered.map((s) => s.startMs)) : 0;
  const spanEnd = ordered.length > 0 ? Math.max(...ordered.map((s) => s.endMs)) : 0;
  const startMs = turn.user?.startMs ?? spanStart;
  const endMs = turn.agent?.endMs ?? Math.max(spanEnd, startMs);

  const waterfall: WaterfallSpan[] = ordered.map((s) => ({
    spanId: s.spanId,
    type: s.type,
    ...(s.name !== undefined ? { name: s.name } : {}),
    ...(s.provider !== undefined ? { provider: s.provider } : {}),
    startMs: s.startMs,
    endMs: s.endMs,
    durationMs: round(Math.max(0, s.endMs - s.startMs)),
    offsetMs: round(Math.max(0, s.startMs - startMs)),
    status: s.status,
    attributes: s.attributes,
    error: s.error,
  }));

  const eou = turn.user?.endMs;
  const firstLlm = ordered.find((s) => s.type === "llm");
  const firstTts = ordered.find((s) => s.type === "tts");
  const firstOutput = ordered.find((s) => s.type === "tts" || s.type === "playout");
  const firstStt = ordered.find((s) => s.type === "stt");

  let ttftMs: number | undefined;
  if (firstLlm && eou !== undefined) {
    const firstTokenMs = numAttr(firstLlm.attributes, "firstTokenMs");
    if (firstTokenMs !== undefined) {
      ttftMs = round(Math.max(0, firstLlm.startMs + firstTokenMs - eou));
    }
  }

  const ttfbMs = firstTts ? numAttr(firstTts.attributes, "firstByteMs") : undefined;

  let responseMs: number | undefined;
  if (firstOutput && eou !== undefined) {
    responseMs = round(Math.max(0, firstOutput.startMs - eou));
  }

  const endpointMs = firstStt ? numAttr(firstStt.attributes, "eouDelayMs") : undefined;

  const toolMs = round(
    ordered
      .filter((s) => s.type === "tool")
      .reduce((sum, s) => sum + Math.max(0, s.endMs - s.startMs), 0),
  );

  const metrics: TurnMetrics = {
    ...(ttftMs !== undefined ? { ttftMs } : {}),
    ...(ttfbMs !== undefined ? { ttfbMs } : {}),
    ...(responseMs !== undefined ? { responseMs } : {}),
    ...(endpointMs !== undefined ? { endpointMs } : {}),
    toolMs,
  };

  return {
    turnId: turn.turnId,
    index: turn.index,
    status: turn.status,
    ...(turn.user?.transcript !== undefined
      ? { userTranscript: turn.user.transcript }
      : {}),
    ...(turn.agent?.transcript !== undefined ? { agentText: turn.agent.transcript } : {}),
    startMs,
    endMs,
    durationMs: round(Math.max(0, endMs - startMs)),
    spans: waterfall,
    metrics,
    interruption: turn.interruption,
    error: turn.error,
  };
}

/** Derive the full call model from an already-validated bundle. */
export function analyzeCall(bundle: TraceBundle): CallAnalysis {
  const spansByTurn = new Map<string, Span[]>();
  for (const span of bundle.spans) {
    const list = spansByTurn.get(span.turnId);
    if (list) {
      list.push(span);
    } else {
      spansByTurn.set(span.turnId, [span]);
    }
  }

  const turns = [...bundle.turns]
    .sort((a, b) => a.index - b.index)
    .map((turn) => analyzeTurn(turn, spansByTurn.get(turn.turnId) ?? []));

  const interruptedTurns = turns.filter((t) => t.status === "interrupted").length;
  const completedTurns = turns.filter((t) => t.status === "completed").length;
  const failedTurns = turns.filter((t) => t.status === "failed").length;
  const noResponseTurns = turns.filter((t) => t.status === "no_response").length;

  const responseValues = turns
    .map((t) => t.metrics.responseMs)
    .filter((v): v is number => v !== undefined);
  const ttftValues = turns
    .map((t) => t.metrics.ttftMs)
    .filter((v): v is number => v !== undefined);

  const p50 = percentile(responseValues, 50);
  const p95 = percentile(responseValues, 95);
  const avgTtft =
    ttftValues.length > 0
      ? round(ttftValues.reduce((a, b) => a + b, 0) / ttftValues.length)
      : undefined;

  const summary: CallSummary = {
    turnCount: turns.length,
    completedTurns,
    interruptedTurns,
    failedTurns,
    noResponseTurns,
    interruptionRate: turns.length > 0 ? interruptedTurns / turns.length : 0,
    ...(p50 !== undefined ? { p50ResponseMs: p50 } : {}),
    ...(p95 !== undefined ? { p95ResponseMs: p95 } : {}),
    ...(avgTtft !== undefined ? { avgTtftMs: avgTtft } : {}),
  };

  return {
    sessionId: bundle.session.sessionId,
    status: bundle.session.status,
    agent: bundle.session.agent,
    providers: bundle.session.providers,
    startedAt: bundle.session.startedAt,
    ...(bundle.session.endedAt !== undefined ? { endedAt: bundle.session.endedAt } : {}),
    ...(bundle.session.durationMs !== undefined
      ? { durationMs: bundle.session.durationMs }
      : {}),
    turns,
    summary,
  };
}

/** Validate an untrusted value, then analyze. Throws on an invalid bundle. */
export function analyzeUnknown(value: unknown): CallAnalysis {
  const result = parseTraceBundle(value);
  if (!result.ok) {
    const reasons = result.error.issues
      .map((i) => `${i.path.join(".")}: ${i.message}`)
      .join("; ");
    throw new Error(`invalid trace bundle: ${reasons}`);
  }
  return analyzeCall(result.bundle);
}
