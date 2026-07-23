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
export type AnalysisLike = components["schemas"]["DerivedAnalysis"];

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
  totalMs: number;
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
  basis: ExplainedOperation["time_basis"],
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
    const leadMs =
      lead != null &&
      typeof lead.value === "number" &&
      Number.isFinite(lead.value) &&
      lead.value >= 0
        ? lead.value
        : null;
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
    return {
      turnId: t.turn_id,
      index,
      stages,
      firstToken: view(t.metrics.first_token_latency),
      generated: view(t.metrics.generated_response_latency),
      response: view(t.metrics.response_latency),
      interrupted: isInterrupted(t),
      hasCascade: hasCascadeChain(stages),
      totalMs: stages.reduce((max, s) => Math.max(max, s.endMs ?? s.startMs ?? 0), 0),
    };
  });
  return { turns, scaleMs: roundUp(Math.max(1, ...turns.map((t) => t.totalMs)), 250) };
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
  name: string;
  value: boolean | number;
  unit: string;
  confidence: string;
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
  events: EventView[];
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

const measurementViews = (measurements: ExplainedMeasurement[]): MeasurementView[] =>
  // Every measurement is carried with its real unit; booleans and other
  // non-duration domains are formatted by their unit at the view layer.
  measurements
    .filter(
      (measurement): measurement is ExplainedMeasurement =>
        typeof measurement.value === "number" || typeof measurement.value === "boolean",
    )
    .map((measurement) => ({
      name: measurement.name,
      value: measurement.value,
      unit: measurement.unit,
      confidence:
        measurement.confidence ?? measurement.evidence?.confidence ?? "unavailable",
    }));

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
      metrics: METRIC_KEYS.map(({ key, label }) => {
        const m = t.metrics[key];
        return {
          key: label,
          value: m?.value ?? null,
          availability: m?.availability ?? "not_observed",
          basis: m?.basis ?? "",
          confidence: m?.confidence ?? "unavailable",
        };
      }),
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

/** Surface the analyzer's diagnoses. Diagnoses come only from the explanation;
 * the viewer never derives one. Evidence ids that name an operation are linked
 * to their turn so the UI can select it. */
export function buildDiagnoses(explanation: ExplanationLike): DiagnosisView[] {
  const turnOfOp = new Map<string, number>();
  explanation.turns.forEach((turn, index) => {
    for (const op of turn.operations) {
      if (op.operation_id != null) turnOfOp.set(op.operation_id, index);
    }
  });
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
export interface UnassignedFacts {
  operations: UnassignedOperationView[];
  measurements: MeasurementView[];
}

/** Surface operations and measurements the analyzer could not scope to a turn
 * (e.g. webrtc jitter/rtt/packet-loss, a device-unavailable operation) so an
 * incident with no turn-scoped evidence is still visible. */
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
