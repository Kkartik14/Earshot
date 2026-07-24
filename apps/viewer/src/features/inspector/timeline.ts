// Visualizes the backend-authored explanation projection. The browser positions
// exact coordinates; it never decides whether an operation is a point or interval.

import type { components } from "../../api/schema";
import { statusTone, type Tone } from "../../lib/status";

// The cascade stages remain a named subset, but they no longer gate what the
// viewer renders: every turn operation is shown. `StageName` stays for the
// cascade-only lead-metric lookup and the optional STT->LLM->TTS projection.
export type StageName = "stt" | "llm" | "tts";

/** Coarse operation class used for colour and grouping. `stt|llm|tts` map to
 * themselves; everything else classifies without inventing a cascade. */
export type OperationRole =
  | "stt"
  | "llm"
  | "tts"
  | "tool"
  | "transport"
  | "render"
  | "vad"
  | "detection"
  | "agent"
  | "other";

const CASCADE_ROLES: readonly OperationRole[] = ["stt", "llm", "tts"];

function classifyRole(operationName: string): OperationRole {
  switch (operationName) {
    case "stt":
    case "llm":
    case "tts":
      return operationName;
    case "tool":
      return "tool";
    case "transport_send":
    case "transport_receive":
      return "transport";
    case "render":
      return "render";
    case "vad":
      return "vad";
    case "turn_detection":
      return "detection";
    case "agent":
    case "agent_response":
      return "agent";
    default:
      return "other";
  }
}

/** The themed CSS custom property that colours a given role. */
export function roleColorVar(role: OperationRole): string {
  switch (role) {
    case "stt":
    case "llm":
    case "tts":
    case "tool":
    case "transport":
    case "render":
    case "vad":
    case "detection":
    case "agent":
      return `var(--${role})`;
    default:
      return "var(--op-other)";
  }
}

/** A short human word for a role, used where no provider/model is present. */
export function roleLabel(role: OperationRole): string {
  switch (role) {
    case "stt":
      return "listen";
    case "llm":
      return "think";
    case "tts":
      return "speak";
    case "tool":
      return "tool call";
    case "transport":
      return "transport";
    case "render":
      return "render";
    case "vad":
      return "voice activity";
    case "detection":
      return "turn detection";
    case "agent":
      return "agent";
    default:
      return "operation";
  }
}

// The lead metric is a cascade-stage concept only; other roles have no lead.
const LEAD_METRIC: Record<StageName, string> = {
  stt: "earshot.stt.ttfb",
  llm: "earshot.llm.ttft",
  tts: "earshot.tts.ttfb",
};
const METRIC_KEYS = [
  { key: "first_token_latency", label: "first_token" },
  { key: "generated_response_latency", label: "generated_response" },
  { key: "sent_response_latency", label: "sent_response" },
  { key: "received_response_latency", label: "received_response" },
  { key: "render_start_response_latency", label: "render_start" },
  { key: "response_latency", label: "response" },
] as const;

type Evidence =
  components["schemas"]["Evidence"] | components["schemas"]["ExplainedEvidence"];
type Timestamp = components["schemas"]["TimePoint"];
export type IncidentLike = components["schemas"]["IncidentBundleJson"];

interface MetricLike {
  value?: number | null;
  availability: string;
  basis: string;
  confidence: string;
}
type AnalysisMetricLike = components["schemas"]["AnalysisMetric"];
export type AnalysisLike = components["schemas"]["DerivedAnalysis"];
export type ContradictionsLike = components["schemas"]["IncidentContradictionsResponse"];

type ExplainedMeasurement = components["schemas"]["ExplainedMeasurement"];
type ExplainedError = components["schemas"]["ExplainedError"];
type ExplainedOperation = components["schemas"]["ExplainedOperation"];
type ExplainedEvent = components["schemas"]["ExplainedEvent"];
type ExplainedTurn = components["schemas"]["ExplainedTurn"];
export type ExplanationLike = components["schemas"]["IncidentExplanation"];

export interface MetricView {
  value: number | null;
  availability: string;
  basis: string;
  confidence: string;
}

/** A rendered operation error (code + category, coloured by tone). */
export interface ErrorView {
  code: string;
  category: string;
  captureClass: string;
}
/** The status/error badge for an operation: shown only when the operation is not
 * in a healthy state, or carries an explicit error. */
export interface StatusView {
  abnormal: boolean;
  tone: Tone;
  label: string;
  error?: ErrorView;
}
/** A resolved graph edge between two operations in the same turn. Edges come
 * only from explicit links or same-trace parent span identity. */
export interface EdgeView {
  fromOperationId: string;
  toOperationId: string;
  relationship: string;
}
/** A single link as carried on an operation; `resolved` is true when the target
 * is another operation within this turn (so it can be drawn as an edge). */
export interface LinkView {
  relationship: string;
  targetOperationId: string | null;
  targetScope: string;
  resolved: boolean;
}

const NON_ABNORMAL_STATUS = new Set([
  "ok",
  "completed",
  "complete",
  "success",
  "succeeded",
  "done",
  // OTel UNSET and an unknown source status assert no failure. Preserve the
  // source label, but do not manufacture an abnormal state from missing proof.
  "unset",
  "unknown",
]);

const errorView = (e: ExplainedError | null | undefined): ErrorView | undefined =>
  e == null
    ? undefined
    : { code: e.code, category: e.category, captureClass: e.capture_class };

/** Whether an operation is abnormal, and how to badge it. An explicit error is a
 * failure (crit); otherwise the tone follows the status word via status.ts. */
export function operationStatus(op: {
  status: string;
  error?: ExplainedError | null;
}): StatusView {
  const error = errorView(op.error);
  const abnormal = error != null || !NON_ABNORMAL_STATUS.has(op.status);
  const tone: Tone = error != null ? "crit" : statusTone(op.status);
  const label = error != null ? `${error.code} · ${error.category}` : op.status;
  return { abnormal, tone, label, error };
}

/** A ± uncertainty in milliseconds from a nanosecond magnitude. BigInt-exact
 * parse; a negative or unparseable value is treated as absent. */
const uncertaintyMs = (value: string | null | undefined): number | null => {
  const n = nano(value);
  if (n == null || n < 0n) return null;
  return Number(n) / 1_000_000;
};

const ACCEPTED_INTERRUPTION_EVENT = "earshot.interruption.accepted";

export interface StageBar {
  operationId: string;
  name: string;
  role: OperationRole;
  provider?: string;
  model?: string;
  startMs: number | null;
  endMs: number | null;
  leadMs: number | null;
  timing: "interval" | "point" | "unavailable";
  confidence?: string;
  status: string;
  statusView: StatusView;
  startUncertaintyMs: number | null;
}
export interface TurnView {
  turnId: string;
  index: number;
  stages: StageBar[];
  firstToken: MetricView;
  generated: MetricView;
  response: MetricView;
  interrupted: boolean;
  /** Whether an STT->LLM->TTS chain is present (optional cascade projection). */
  hasCascade: boolean;
  totalMs: number | null;
}
export interface Timeline {
  turns: TurnView[];
  scaleMs: number;
}

const nano = (value: string | null | undefined): bigint | null => {
  if (value == null) return null;
  try {
    return BigInt(value);
  } catch {
    return null;
  }
};

const durationMs = (
  start: Timestamp | null | undefined,
  end: Timestamp | null | undefined,
) => {
  const startNano = nano(start?.monotonic_time_nano);
  const endNano = nano(end?.monotonic_time_nano);
  if (startNano == null || endNano == null || endNano < startNano) return null;
  if (start?.clock_domain_id == null || start.clock_domain_id !== end?.clock_domain_id) {
    return null;
  }
  return Number(endNano - startNano) / 1_000_000;
};

const view = (m: MetricLike | undefined): MetricView => ({
  value: m?.value ?? null,
  availability: m?.availability ?? "not_observed",
  basis: m?.basis ?? "",
  confidence: m?.confidence ?? "unavailable",
});

const roundUp = (value: number, step: number): number =>
  Math.max(step, Math.ceil(value / step) * step);

interface OperationWindow {
  op: ExplainedOperation;
  operationId: string;
  name: string;
  role: OperationRole;
  provider?: string;
  model?: string;
  origin: ExplainedOperation | null;
  startMs: number | null;
  endMs: number | null;
  leadMs: number | null;
  timing: "interval" | "point" | "unavailable";
  confidence?: string;
}

const coordinateDeltaMs = (
  value: string,
  // Widened to a bare string so analysis-authored coordinates (interruption
  // stages) can be placed by the same rule: an offset exists only when the basis
  // and clock domain match the origin's exactly.
  basis: string | null | undefined,
  domain: string | null | undefined,
  origin: ExplainedOperation | null,
): number | null => {
  if (
    origin == null ||
    basis !== origin.time_basis ||
    domain !== origin.clock_domain_id
  ) {
    return null;
  }
  const coordinate = nano(value);
  const originCoordinate = nano(origin.start_nano);
  if (coordinate == null || originCoordinate == null) return null;
  return Number(coordinate - originCoordinate) / 1_000_000;
};

/** Normalize a cascade lead measurement (STT/LLM TTFB, LLM TTFT) to milliseconds
 * by its declared unit. The lead is a provider scalar whose unit is NOT fixed to
 * ms: the measurement contract permits provider-specific units, and sibling
 * producers report the same latencies in seconds (Pipecat `metrics.ttfb` and
 * LiveKit `*_latency` both carry `unit="s"`). Reading `value` as raw ms would be
 * 1000x wrong for a seconds lead. An unexpected unit is left unavailable rather
 * than silently mislabeled as milliseconds. */
const leadMeasurementMs = (lead: ExplainedMeasurement | undefined): number | null => {
  if (
    lead == null ||
    typeof lead.value !== "number" ||
    !Number.isFinite(lead.value) ||
    lead.value < 0
  ) {
    return null;
  }
  switch (lead.unit) {
    case "ms":
      return lead.value;
    case "s":
      return lead.value * 1000;
    default:
      return null;
  }
};

/** Placement over EVERY operation in the turn, using the analyzer's facts.
 * The clock-alignment origin is computed across all operations (not just the
 * cascade), preserving the "leave the whole turn unplaced if any op is
 * unaligned" discipline so an arbitrary first arrival never reads as +0 ms. */
function computeOperations(turn: ExplainedTurn): OperationWindow[] {
  const candidates = turn.operations;
  const coordinateGroups = new Set(
    candidates.map(
      (operation) => `${operation.time_basis}\u0000${operation.clock_domain_id ?? ""}`,
    ),
  );
  const comparable =
    coordinateGroups.size === 1 &&
    candidates.every(
      (operation) =>
        operation.clock_domain_id != null && nano(operation.start_nano) != null,
    );
  const ops = comparable
    ? [...candidates].sort((left, right) => {
        const leftStart = nano(left.start_nano);
        const rightStart = nano(right.start_nano);
        if (leftStart == null || rightStart == null) return 0;
        if (leftStart < rightStart) return -1;
        if (leftStart > rightStart) return 1;
        return left.operation_id.localeCompare(right.operation_id);
      })
    : candidates;
  // A single origin would falsely place independent clocks on one axis. If any
  // operation is unaligned, leave the whole turn unplaced instead of making
  // whichever operation happened to arrive first look like +0 ms.
  const origin = comparable ? (ops[0] ?? null) : null;

  return ops.map((op) => {
    const role = classifyRole(op.operation_name);
    const operationId = op.operation_id;
    const startMs = coordinateDeltaMs(
      op.start_nano,
      op.time_basis,
      op.clock_domain_id,
      origin,
    );
    // The lead metric is a cascade-stage concept; other roles carry none.
    const leadMetricName =
      role === "stt" || role === "llm" || role === "tts" ? LEAD_METRIC[role] : undefined;
    const lead =
      leadMetricName != null
        ? op.measurements.find((item) => item.name === leadMetricName)
        : undefined;
    const duration = nano(op.duration_nano);
    const explicitDuration =
      op.shape === "interval" && duration != null && duration >= 0n
        ? Number(duration) / 1_000_000
        : null;
    const endMs =
      startMs != null && explicitDuration != null ? startMs + explicitDuration : null;
    const timing =
      startMs == null ? "unavailable" : op.shape === "interval" ? "interval" : "point";
    const leadMs = leadMeasurementMs(lead);
    return {
      op,
      operationId,
      name: op.operation_name,
      role,
      provider: op.provider ?? undefined,
      model: op.model ?? undefined,
      origin,
      startMs,
      endMs,
      leadMs,
      timing,
      confidence: lead?.evidence?.confidence ?? undefined,
    };
  });
}

/** Whether the turn contains a full STT->LLM->TTS cascade. The cascade is an
 * optional derived view; the default rendering is the generic operation list. */
export function hasCascadeChain(operations: { role: OperationRole }[]): boolean {
  return CASCADE_ROLES.every((role) => operations.some((op) => op.role === role));
}

function isInterrupted(turn: ExplainedTurn): boolean {
  return turn.events.some((event) => event.event_name === ACCEPTED_INTERRUPTION_EVENT);
}

export function buildTimeline(explanation: ExplanationLike): Timeline {
  const turns: TurnView[] = explanation.turns.map((t, index) => {
    const windows = computeOperations(t);
    const stages: StageBar[] = windows.map((w) => ({
      operationId: w.operationId,
      name: w.name,
      role: w.role,
      provider: w.provider,
      model: w.model,
      startMs: w.startMs,
      endMs: w.endMs,
      leadMs: w.leadMs,
      timing: w.timing,
      confidence: w.confidence,
      status: typeof w.op.status === "string" ? w.op.status : "unknown",
      statusView: operationStatus(w.op),
      startUncertaintyMs: uncertaintyMs(w.op.start_uncertainty_nano),
    }));
    const placedBoundaries = stages
      .map((stage) => stage.endMs ?? stage.startMs)
      .filter((value): value is number => value != null);
    return {
      turnId: t.turn_id,
      index,
      stages,
      firstToken: view(t.metrics.first_token_latency),
      generated: view(t.metrics.generated_response_latency),
      response: view(t.metrics.response_latency),
      interrupted: isInterrupted(t),
      hasCascade: hasCascadeChain(stages),
      totalMs: placedBoundaries.length > 0 ? Math.max(...placedBoundaries) : null,
    };
  });
  const knownDurations = turns
    .map((turn) => turn.totalMs)
    .filter((value): value is number => value != null);
  return { turns, scaleMs: roundUp(Math.max(1, ...knownDurations), 250) };
}

// -- drawer detail ----------------------------------------------------------

export interface EvidenceView {
  source: string;
  observer: string;
  method: string;
  confidence: string;
  sourceField?: string;
}
export interface MeasurementView {
  reactKey: string;
  name: string;
  value: boolean | number;
  unit: string;
  confidence: string;
  aggregation: string;
  basis: string;
  limitation?: string;
  evidenceIds: string[];
  sourceField?: string;
}
export interface StageDetail {
  operationId: string;
  name: string;
  role: OperationRole;
  provider?: string;
  model?: string;
  status: string;
  statusView: StatusView;
  startMs: number | null;
  endMs: number | null;
  leadMs: number | null;
  timing: "interval" | "point" | "unavailable";
  startUncertaintyMs: number | null;
  endUncertaintyMs: number | null;
  /** All links this operation carries, resolved or not. Resolved ones are drawn
   * as edges by the call graph; unresolved (external/unknown) ones surface as a
   * relationship tag on the node. */
  links: LinkView[];
  /** An interruption event that explicitly references this operation. When set,
   * the interruption attaches here rather than remaining at turn level. */
  interruptedByEvent?: string;
  evidence?: EvidenceView;
  measurements: MeasurementView[];
}
export interface EventView {
  name: string;
  atMs: number | null;
  participant: string;
  confidence: string;
  /** The operation this event explicitly identifies; otherwise null (a
   * turn-level event). */
  attachedOperationId: string | null;
}
export interface MetricRow {
  key: string;
  value: number | null;
  availability: string;
  basis: string;
  confidence: string;
  /** The analyzer's reason the metric is not `available`, carried so the UI can
   * name the obstacle instead of leaving a blank where a number would be. */
  limitation: string | null;
}

const metricRow = (key: string, metric: AnalysisMetricLike | undefined): MetricRow => ({
  key,
  value: metric?.value ?? null,
  availability: metric?.availability ?? "not_observed",
  basis: metric?.basis ?? "",
  confidence: metric?.confidence ?? "unavailable",
  limitation: metric?.limitation ?? null,
});

/** One canonical stage of an interruption teardown, exactly as the analyzer
 * observed (or failed to observe) it. */
export interface InterruptionStageView {
  stage: string;
  observed: boolean;
  /** Offset from the turn's placement origin, present only when the stage's
   * coordinate is comparable to it. */
  atMs: number | null;
  /** The exact recorded coordinate, kept for stages that cannot be placed on the
   * turn axis so the fact survives even when the offset does not. */
  coordinate: string | null;
  evidenceId: string | null;
  /** Why an unobserved stage is absent. Never rendered as a zero or a success. */
  coverageReason: string | null;
  outcome: string | null;
}

/** One interruption episode's ordered causal chain and its barge-in latency. */
export interface InterruptionChainView {
  reactKey: string;
  turnId: string;
  /** `accepted` | `false` | `ignored` | `unknown`, authored by the analyzer. */
  classification: string;
  stages: InterruptionStageView[];
  /** Overlap -> render-stop latency. Unavailable when either endpoint was not
   * observed or the two are not comparable; the row then states which. */
  effectiveness: MetricRow;
}
export interface CoverageRow {
  signal: string;
  availability: string;
  reason?: string;
}
export interface TurnDetail {
  turnId: string;
  index: number;
  interrupted: boolean;
  hasCascade: boolean;
  firstTokenMs: number | null;
  stages: StageDetail[];
  /** Resolved parent/link edges between operations in this turn. */
  edges: EdgeView[];
  metrics: MetricRow[];
  measurements: MeasurementView[];
  events: EventView[];
  /** One chain per interruption episode observed in this turn; empty when none
   * was observed. The viewer never derives a chain — it renders the analyzer's. */
  interruptionChains: InterruptionChainView[];
}

/** Project the analyzer's interruption chains for one turn. Stage coordinates are
 * placed on the turn axis only when they are comparable to its origin; otherwise
 * the exact recorded coordinate is kept instead of an invented offset. */
function interruptionChainViews(
  turn: ExplainedTurn,
  origin: ExplainedOperation | null,
): InterruptionChainView[] {
  return (turn.interruption_chains ?? []).map((chain, index) => ({
    reactKey: `${turn.turn_id}:${index}`,
    turnId: chain.turn_id,
    classification: chain.classification,
    stages: chain.stages.map((stage) => ({
      stage: stage.stage,
      observed: stage.observed,
      atMs:
        stage.at_nano == null
          ? null
          : coordinateDeltaMs(
              stage.at_nano,
              stage.time_basis,
              stage.clock_domain_id,
              origin,
            ),
      coordinate:
        stage.at_nano == null
          ? null
          : `${stage.clock_domain_id ?? "unknown clock"} · ${stage.time_basis ?? "unknown basis"} · ${stage.at_nano}ns`,
      evidenceId: stage.evidence_id ?? null,
      coverageReason: stage.coverage_reason ?? null,
      outcome: stage.outcome ?? null,
    })),
    effectiveness: metricRow("effectiveness", chain.effectiveness),
  }));
}

const evidenceView = (e: Evidence | null | undefined): EvidenceView | undefined =>
  e == null
    ? undefined
    : {
        source: e.source ?? "unknown",
        observer: e.observer ?? "unknown",
        method: e.method ?? "unknown",
        confidence: e.confidence ?? "unavailable",
        sourceField: e.source_field ?? undefined,
      };

const measurementViews = (measurements: ExplainedMeasurement[]): MeasurementView[] => {
  const occurrences = new Map<string, number>();
  // Every measurement is carried with its exact owner/provenance identity and
  // real unit. Repeated snapshots remain distinct facts.
  return measurements
    .filter(
      (measurement): measurement is ExplainedMeasurement =>
        typeof measurement.value === "number" || typeof measurement.value === "boolean",
    )
    .map((measurement) => {
      const evidenceIds = Array.isArray(measurement.evidence_ids)
        ? measurement.evidence_ids
        : [];
      const identity = JSON.stringify([
        evidenceIds,
        measurement.name,
        measurement.unit,
        measurement.aggregation,
        typeof measurement.value,
        measurement.value,
      ]);
      const occurrence = occurrences.get(identity) ?? 0;
      occurrences.set(identity, occurrence + 1);
      return {
        reactKey: `${identity}:${occurrence}`,
        name: measurement.name,
        value: measurement.value,
        unit: measurement.unit,
        confidence:
          measurement.confidence ?? measurement.evidence?.confidence ?? "unavailable",
        aggregation: measurement.aggregation ?? "unknown",
        basis: measurement.basis ?? "provider_measurement",
        limitation: measurement.limitation ?? undefined,
        evidenceIds: [...evidenceIds],
        sourceField: measurement.evidence?.source_field ?? undefined,
      };
    });
};

/** Resolve source-authored links and same-trace parent span identities against
 * operations actually present in this turn. External and unresolved targets stay
 * as node-level tags. No arrival-order or stage-order edge is ever invented. */
function resolveLinks(windows: OperationWindow[]): {
  linksByOp: Map<string, LinkView[]>;
  edges: EdgeView[];
} {
  const presentIds = new Set(windows.map((window) => window.op.operation_id));
  const operationBySpan = new Map<string, OperationWindow>();
  const spanKey = (
    traceId: string | null | undefined,
    spanId: string | null | undefined,
  ) => (traceId != null && spanId != null ? `${traceId}:${spanId}` : null);
  for (const window of windows) {
    const key = spanKey(window.op.trace_id, window.op.span_id);
    if (key != null) operationBySpan.set(key, window);
  }
  const linksByOp = new Map<string, LinkView[]>();
  const edges: EdgeView[] = [];
  for (const w of windows) {
    const views: LinkView[] = [];
    for (const link of w.op.links ?? []) {
      const targetScope = link.target_scope ?? "unknown";
      const spanTargetKey =
        targetScope === "internal" ? spanKey(link.trace_id, link.span_id) : null;
      const spanTarget =
        spanTargetKey == null ? undefined : operationBySpan.get(spanTargetKey);
      const target = link.target_operation_id ?? spanTarget?.operationId ?? null;
      const resolved =
        targetScope !== "external" && target != null && presentIds.has(target);
      views.push({
        relationship: link.relationship,
        targetOperationId: target,
        targetScope,
        resolved,
      });
      if (resolved && target != null) {
        edges.push({
          fromOperationId: w.operationId,
          toOperationId: target,
          relationship: link.relationship,
        });
      }
    }
    if (w.op.parent_scope !== "external") {
      const parentKey = spanKey(w.op.trace_id, w.op.parent_span_id);
      const parent = parentKey == null ? undefined : operationBySpan.get(parentKey);
      if (parent != null) {
        edges.push({
          fromOperationId: parent.operationId,
          toOperationId: w.operationId,
          relationship: "parent",
        });
      }
    }
    linksByOp.set(w.operationId, views);
  }
  return { linksByOp, edges };
}

/** Attach events only through their source-authored operation identity. Stream
 * correlation is not causal evidence, so stream-only events remain turn-level. */
function attachEventsToOps(windows: OperationWindow[], events: ExplainedEvent[]) {
  const operationIds = new Set(windows.map((window) => window.operationId));
  const eventTarget = (event: ExplainedEvent): string | null => {
    return event.operation_id != null && operationIds.has(event.operation_id)
      ? event.operation_id
      : null;
  };
  // For each operation, the interruption event (if any) that attaches to it.
  const interruptByOp = new Map<string, string>();
  for (const event of events) {
    const target = eventTarget(event);
    if (target != null && event.event_name === ACCEPTED_INTERRUPTION_EVENT) {
      interruptByOp.set(target, event.event_name);
    }
  }
  return { eventTarget, interruptByOp };
}

export function buildTurnDetails(explanation: ExplanationLike): TurnDetail[] {
  return explanation.turns.map((t, index) => {
    const windows = computeOperations(t);
    const origin = windows[0]?.origin ?? null;
    const { linksByOp, edges } = resolveLinks(windows);
    const { eventTarget, interruptByOp } = attachEventsToOps(windows, t.events);
    return {
      turnId: t.turn_id,
      index,
      interrupted: isInterrupted(t),
      hasCascade: hasCascadeChain(windows),
      firstTokenMs: t.metrics.first_token_latency?.value ?? null,
      edges,
      stages: windows.map((w) => ({
        operationId: w.operationId,
        name: w.name,
        role: w.role,
        provider: w.provider,
        model: w.model,
        status: typeof w.op.status === "string" ? w.op.status : "unknown",
        statusView: operationStatus(w.op),
        startMs: w.startMs,
        endMs: w.endMs,
        leadMs: w.leadMs,
        timing: w.timing,
        startUncertaintyMs: uncertaintyMs(w.op.start_uncertainty_nano),
        endUncertaintyMs: uncertaintyMs(w.op.end_uncertainty_nano),
        links: linksByOp.get(w.operationId) ?? [],
        interruptedByEvent: interruptByOp.get(w.operationId),
        evidence: evidenceView(w.op.evidence),
        measurements: measurementViews(w.op.measurements),
      })),
      metrics: METRIC_KEYS.map(({ key, label }) => metricRow(label, t.metrics[key])),
      interruptionChains: interruptionChainViews(t, origin),
      measurements: measurementViews(t.measurements ?? []),
      events: t.events.map((event) => ({
        name: event.event_name,
        atMs: coordinateDeltaMs(
          event.at_nano,
          event.time_basis,
          event.clock_domain_id,
          origin,
        ),
        participant: (event.participant_id ?? "").split("-").pop() ?? "",
        confidence: event.evidence?.confidence ?? "",
        attachedOperationId: eventTarget(event),
      })),
    };
  });
}

// -- session-level facts ----------------------------------------------------

/** A backend-authored diagnosis, with each evidence id resolved to the turn that
 * contains the referenced operation (when it is an operation). */
export interface DiagnosisView {
  id: string;
  code: string;
  summary: string;
  confidence: string;
  limitations: string[];
  evidence: { id: string; turnIndex: number | null }[];
}

/** Index every operation id to the turn that contains it, so an evidence id that
 * names an operation can be made selectable and one that does not stays inert. */
function operationTurnIndex(explanation: ExplanationLike): Map<string, number> {
  const turnOfOp = new Map<string, number>();
  explanation.turns.forEach((turn, index) => {
    for (const op of turn.operations) {
      if (op.operation_id != null) turnOfOp.set(op.operation_id, index);
    }
  });
  return turnOfOp;
}

/** Surface the analyzer's diagnoses. Diagnoses come only from the explanation;
 * the viewer never derives one. Evidence ids that name an operation are linked
 * to their turn so the UI can select it. */
export function buildDiagnoses(explanation: ExplanationLike): DiagnosisView[] {
  const turnOfOp = operationTurnIndex(explanation);
  return (explanation.diagnoses ?? []).map((d) => ({
    id: d.diagnosis_id,
    code: d.code,
    summary: d.summary,
    confidence: d.confidence,
    limitations: d.limitations ?? [],
    evidence: d.evidence_ids.map((id) => ({
      id,
      turnIndex: turnOfOp.has(id) ? (turnOfOp.get(id) as number) : null,
    })),
  }));
}

// -- contradictions ---------------------------------------------------------

/** One backend-detected contradiction, with its cited evidence resolved to the
 * turn that owns it where the id names an operation. */
export interface ContradictionView {
  reactKey: string;
  kind: string;
  summary: string;
  boundary: string | null;
  turnId: string | null;
  evidence: { id: string; turnIndex: number | null }[];
}

/** Project the backend's contradiction report. Contradictions are detected by the
 * backend against a named evidence digest; the viewer only resolves their evidence
 * ids to turns and never decides that two observations conflict. */
export function buildContradictions(
  explanation: ExplanationLike,
  report: ContradictionsLike,
): ContradictionView[] {
  const turnOfOp = operationTurnIndex(explanation);
  return report.contradictions.map((item, index) => ({
    reactKey: `${item.kind}:${item.subject ?? ""}:${index}`,
    kind: item.kind,
    summary: item.summary,
    boundary: item.boundary ?? null,
    turnId: item.turn_id ?? null,
    evidence: item.evidence_ids.map((id) => ({
      id,
      turnIndex: turnOfOp.has(id) ? (turnOfOp.get(id) as number) : null,
    })),
  }));
}

// -- clock comparability ----------------------------------------------------

/** The analyzer's basis for a latency it derived through a declared calibration. */
const CALIBRATED_BASIS = "cross_clock_calibrated";

/** Plain-language readings of the analyzer's clock-related limitation codes. Each
 * says what stopped the comparison; none implies the latency was zero or fine. */
const CLOCK_LIMITATION_NOTE: Record<string, string> = {
  cross_clock_domain: "two clock domains with no declared calibration between them",
  cross_clock_ambiguous: "declared calibrations disagree beyond their uncertainty",
  calibrated_time_reversed: "the calibrated interval runs backwards",
  same_domain_time_reversed: "the recorded interval runs backwards",
  timestamp_representation_unavailable: "the two facts share no timestamp representation",
};

/** How a metric's cross-clock story reads, or `null` when clocks are not the
 * reason it reads as it does (a same-domain measurement, or an absence with an
 * unrelated cause). */
export function clockComparability(
  metric: Pick<MetricRow, "availability" | "basis" | "limitation">,
): { state: "estimated" | "unavailable"; note: string } | null {
  if (metric.availability === "available") {
    return metric.basis === CALIBRATED_BASIS
      ? {
          state: "estimated",
          note: "estimated across clock domains through a declared calibration",
        }
      : null;
  }
  const note =
    metric.limitation == null ? null : CLOCK_LIMITATION_NOTE[metric.limitation];
  return note == null ? null : { state: "unavailable", note };
}

export interface ClockDomainView {
  id: string;
  kind: string;
  observer: string;
  /** The domain's own declared error bound; `null` means it declares none. */
  uncertaintyMs: number | null;
}
export interface ClockRelationView {
  relationId: string;
  fromDomain: string;
  toDomain: string;
  method: string;
  /** The calibration's own error bound, which every latency estimated through it
   * carries. `null` means the relation declares none — unknown, never zero. */
  uncertaintyMs: number | null;
  driftPpm: number | null;
}
export interface CrossClockMetricView {
  reactKey: string;
  turnIndex: number;
  turnId: string;
  metric: string;
  state: "estimated" | "unavailable";
  availability: string;
  note: string;
}
export interface ClockCalibrationView {
  domains: ClockDomainView[];
  relations: ClockRelationView[];
  /** Every turn latency whose value — or absence — is decided by clock
   * comparability, so a cross-clock gap is never a silently missing number. */
  crossClock: CrossClockMetricView[];
}

/** Assemble the session's clock-comparability picture: the declared domains and
 * calibrations from the source artifact, and every derived latency those
 * calibrations either produced (estimated) or could not produce (unavailable). */
export function buildClockCalibration(
  incident: IncidentLike,
  details: TurnDetail[],
): ClockCalibrationView {
  const domains = (incident.profile.clock_domains ?? []).map((domain) => ({
    id: domain.clock_domain_id,
    kind: domain.kind,
    observer: domain.observer,
    uncertaintyMs: uncertaintyMs(domain.uncertainty_nano),
  }));
  const relations = (incident.profile.clock_relations ?? []).map((relation) => ({
    relationId: relation.relation_id,
    fromDomain: relation.from_clock_domain_id,
    toDomain: relation.to_clock_domain_id,
    method: relation.method,
    uncertaintyMs: uncertaintyMs(relation.uncertainty_nano),
    driftPpm: relation.drift_ppm ?? null,
  }));

  const crossClock: CrossClockMetricView[] = [];
  const record = (detail: TurnDetail, label: string, metric: MetricRow) => {
    const reading = clockComparability(metric);
    if (reading == null) return;
    crossClock.push({
      reactKey: `${detail.turnId}:${label}`,
      turnIndex: detail.index,
      turnId: detail.turnId,
      metric: label,
      state: reading.state,
      availability: metric.availability,
      note: reading.note,
    });
  };
  for (const detail of details) {
    for (const metric of detail.metrics) record(detail, metric.key, metric);
    detail.interruptionChains.forEach((chain, index) =>
      record(detail, `interruption ${index} · effectiveness`, chain.effectiveness),
    );
  }
  return { domains, relations, crossClock };
}

/** How a media file's own timeline relates to the incident's, if at all.
 *
 *  Media synchronization is not a second mechanism: a media file's timeline is
 *  another clock domain, so this is the ordinary cross-clock question answered by
 *  the ordinary `ClockRelation`. `aligned` reports the declared calibration and
 *  its own error bound; `unaligned` refuses to guess an offset. */
export type MediaAlignmentView =
  | { state: "session_domain"; note: string }
  | {
      state: "aligned";
      note: string;
      method: string;
      uncertaintyMs: number | null;
      driftPpm: number | null;
    }
  | { state: "unaligned"; note: string }
  | { state: "undeclared"; note: string };

/** The declared retention governing externally-held media. Earshot records the
 *  policy; it does not enforce it, because it does not hold the bytes. */
export interface MediaRetentionView {
  expiresAtUnixNano: string | null;
  ttlMs: number | null;
  policyId: string | null;
}

/** One media reference, as custody facts only. There is deliberately no field
 *  carrying media bytes, a duration derived from them, or anything earshot could
 *  only know by reading them: earshot never does. */
export interface MediaCustodyView {
  mediaId: string;
  mediaKind: string;
  contentType: string;
  /** Who holds the bytes. `null` renders as "not declared", never as earshot. */
  custodian: string | null;
  integrity: "content_digest" | "opaque_handle";
  /** The declared digest, if the reference carries one. Never computed here. */
  digest: string | null;
  sizeBytes: number | null;
  /** The plain-language integrity claim, written so it cannot be read as
   *  "earshot checked this". */
  integrityNote: string;
  coveredMs: number | null;
  coveredNote: string;
  consent: string | null;
  retention: MediaRetentionView | null;
  alignment: MediaAlignmentView;
  /** A custodian URL for a user-initiated, direct hand-off. It is never used as
   *  a media `src`: an `src` would make the viewer fetch the bytes on render. */
  locatorUri: string | null;
  locatorExpiresNano: string | null;
}

/** Assemble the custody panel: where externally-held media lives, whether anyone
 *  measured it, and whether a declared calibration can place it on this
 *  session's timeline. Reads only `profile.media_refs` and the clock records —
 *  it dereferences nothing. */
export function buildMediaCustody(incident: IncidentLike): MediaCustodyView[] {
  const relations = incident.profile.clock_relations ?? [];
  // The domains this session's own evidence is recorded in: the set media has to
  // reach to be overlayable at all.
  const observationDomains = new Set<string>();
  for (const op of incident.profile.operations ?? []) {
    for (const point of [op.started_at, op.ended_at]) {
      if (point?.clock_domain_id != null) observationDomains.add(point.clock_domain_id);
    }
  }
  for (const event of incident.profile.events ?? []) {
    if (event.time?.clock_domain_id != null)
      observationDomains.add(event.time.clock_domain_id);
  }

  return (incident.profile.media_refs ?? []).map((media) => {
    const integrity =
      media.integrity === "opaque_handle" ? "opaque_handle" : "content_digest";
    const digest = media.sha256 ?? null;
    const covered = durationMs(media.time_range?.start, media.time_range?.end);
    return {
      mediaId: media.media_id,
      mediaKind: media.media_kind,
      contentType: media.content_type,
      custodian: media.custodian ?? null,
      integrity,
      digest,
      sizeBytes: media.size_bytes ?? null,
      integrityNote:
        integrity === "content_digest"
          ? "digest declared by the producer — earshot did not read these bytes and has not verified it"
          : "no digest — earshot never read these bytes and cannot attest to their integrity",
      coveredMs: covered,
      coveredNote:
        media.time_range == null
          ? "no covered window declared"
          : covered == null
            ? "covered window declared, but its endpoints are not comparable"
            : "covered window",
      consent: media.consent?.status ?? null,
      retention: retentionView(media.retention),
      alignment: mediaAlignment(media.clock_domain_id, observationDomains, relations),
      locatorUri: media.locator?.uri ?? null,
      locatorExpiresNano: media.locator?.expires_at_unix_nano ?? null,
    };
  });
}

const retentionView = (
  retention: components["schemas"]["RetentionPolicy"] | null | undefined,
): MediaRetentionView | null => {
  if (retention == null) return null;
  const ttl = nano(retention.ttl_nano);
  return {
    expiresAtUnixNano: retention.expires_at_unix_nano ?? null,
    ttlMs: ttl == null || ttl < 0n ? null : Number(ttl) / 1_000_000,
    policyId: retention.policy_id ?? null,
  };
};

/** Decide media alignment with the same rule the analyzer's aligner uses: the
 *  media domain is either one the session records in, or a single declared
 *  relation joins it to one (in either direction — a calibration is an
 *  invertible affine map). Chains are not composed, so a media file two hops away
 *  is reported unaligned rather than aligned by an offset nothing computed. */
function mediaAlignment(
  mediaDomain: string | null | undefined,
  observationDomains: Set<string>,
  relations: components["schemas"]["ClockRelation"][],
): MediaAlignmentView {
  if (mediaDomain == null)
    return {
      state: "undeclared",
      note: "this reference declares no media timeline, so it cannot be placed on the session's",
    };
  if (observationDomains.has(mediaDomain))
    return {
      state: "session_domain",
      note: "recorded in a clock domain this session already uses",
    };
  const relation = relations.find(
    (r) =>
      (r.from_clock_domain_id === mediaDomain &&
        observationDomains.has(r.to_clock_domain_id)) ||
      (r.to_clock_domain_id === mediaDomain &&
        observationDomains.has(r.from_clock_domain_id)),
  );
  if (relation == null)
    return {
      state: "unaligned",
      note: "no declared clock relation reaches this session's timeline, so no offset is assumed",
    };
  return {
    state: "aligned",
    note: `via ${relation.relation_id}`,
    method: relation.method,
    uncertaintyMs: uncertaintyMs(relation.uncertainty_nano),
    driftPpm: relation.drift_ppm ?? null,
  };
}

/** A session-level operation whose evidence is not turn-scoped (e.g. a
 * `device_unavailable` op). It has no shared turn axis, so only a self-contained
 * observed duration (from `duration_nano`) is shown — never a cross-op offset. */
export interface UnassignedOperationView {
  operationId: string;
  name: string;
  role: OperationRole;
  status: string;
  statusView: StatusView;
  durationMs: number | null;
  measurements: MeasurementView[];
}
export interface UnassignedEventView {
  eventId: string;
  name: string;
  coordinate: string;
  confidence: string;
}
export interface UnassignedFacts {
  operations: UnassignedOperationView[];
  events: UnassignedEventView[];
  measurements: MeasurementView[];
}

/** Surface operations/events without a turn and measurements without either a
 * turn or operation owner, so genuinely session-level evidence stays visible. */
export function buildUnassigned(explanation: ExplanationLike): UnassignedFacts {
  const operations = (explanation.unassigned_operations ?? []).map((op, index) => {
    const duration = nano(op.duration_nano);
    return {
      operationId: op.operation_id ?? `${op.operation_name}-${index}`,
      name: op.operation_name,
      role: classifyRole(op.operation_name),
      status: typeof op.status === "string" ? op.status : "unknown",
      statusView: operationStatus(op),
      durationMs:
        op.shape === "interval" && duration != null && duration >= 0n
          ? Number(duration) / 1_000_000
          : null,
      measurements: measurementViews(op.measurements),
    };
  });
  return {
    operations,
    events: (explanation.unassigned_events ?? []).map((event) => ({
      eventId: event.event_id,
      name: event.event_name,
      coordinate: `${event.clock_domain_id ?? "unknown clock"} · ${event.time_basis} · ${event.at_nano}ns`,
      confidence: event.evidence?.confidence ?? "unknown",
    })),
    measurements: measurementViews(explanation.unassigned_measurements ?? []),
  };
}

export function getCoverage(explanation: ExplanationLike): CoverageRow[] {
  const coverage = explanation.coverage
    .filter((c) => c.availability !== "available")
    .map((c) => ({
      signal: c.signal,
      availability: c.availability,
      reason: c.reason ?? undefined,
    }));
  const omissions = explanation.omissions.map((item) => ({
    signal: `privacy.${item.capture_class}`,
    availability: "omitted",
    reason: item.reason,
  }));
  const limitations = explanation.limitations.map((limitation) => ({
    signal: `analysis.${limitation}`,
    availability: "unavailable",
    reason: limitation,
  }));
  return [...coverage, ...omissions, ...limitations];
}

export interface SessionSummary {
  sessionId: string;
  status: string;
  stack: string[];
  turns: number;
  durationMs: number | null;
  p95FirstTokenMs: number | null;
  interruptions: number;
}

/** Nearest-rank percentile; null for an empty sample. */
function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.ceil((p / 100) * sorted.length);
  return sorted[Math.min(sorted.length - 1, Math.max(0, rank - 1))];
}

export function buildSummary(
  incident: IncidentLike,
  explanation: ExplanationLike,
  timeline: Timeline,
): SessionSummary {
  const stack: string[] = [];
  const seen = new Set<string>();
  for (const turn of timeline.turns) {
    for (const stage of turn.stages) {
      // Only provider-bearing operations belong in the technology stack; other
      // operations (transport, render, tool, …) would add "? · ?" noise.
      if (stage.provider == null && stage.model == null) continue;
      const label = `${stage.provider ?? "?"} · ${stage.model ?? "?"}`;
      if (!seen.has(label)) {
        seen.add(label);
        stack.push(label);
      }
    }
  }
  const firstTokens = timeline.turns
    .map((t) => t.firstToken.value)
    .filter((v): v is number => v != null);
  const sessionDuration = durationMs(
    incident.profile.session?.started_at,
    incident.profile.session?.ended_at,
  );

  return {
    sessionId: incident.profile.manifest?.session_id ?? "session",
    status: explanation.session_status,
    stack,
    turns: timeline.turns.length,
    durationMs: sessionDuration,
    p95FirstTokenMs: percentile(firstTokens, 95),
    interruptions: timeline.turns.filter((t) => t.interrupted).length,
  };
}
