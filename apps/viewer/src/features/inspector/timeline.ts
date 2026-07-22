// Visualizes the backend-authored explanation projection. The browser positions
// exact coordinates; it never decides whether an operation is a point or interval.

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
const METRIC_KEYS: { key: string; label: string }[] = [
  { key: "first_token_latency", label: "first_token" },
  { key: "generated_response_latency", label: "generated_response" },
  { key: "sent_response_latency", label: "sent_response" },
  { key: "received_response_latency", label: "received_response" },
  { key: "render_start_response_latency", label: "render_start" },
  { key: "response_latency", label: "response" },
];

interface Evidence {
  source?: string | null;
  observer?: string | null;
  method?: string | null;
  confidence?: string | null;
  source_field?: string | null;
}
interface Operation {
  operation_id?: string | null;
  operation_name: string;
  turn_id?: string | null;
  started_at: Timestamp;
  ended_at?: Timestamp | null;
  attributes?: Record<string, unknown> | null;
  evidence?: Evidence | null;
  status?: string | null;
}
interface Timestamp {
  monotonic_time_nano?: string | null;
  clock_domain_id?: string | null;
}
interface QualitySample {
  measurements: { name: string; value: number; unit: string }[];
  attributes?: Record<string, unknown> | null;
  evidence?: { confidence?: string | null } | null;
}
interface EventRecord {
  event_name: string;
  turn_id?: string | null;
  time?: Timestamp | null;
  participant_id?: string | null;
  evidence?: { confidence?: string | null } | null;
}
interface CoverageRecord {
  signal: string;
  availability: string;
  reason?: string | null;
}
export interface IncidentLike {
  profile: {
    manifest?: { session_id?: string } | null;
    session?: {
      status?: string;
      started_at?: Timestamp;
      ended_at?: Timestamp | null;
    } | null;
    operations: Operation[];
    events: EventRecord[];
    quality_samples: QualitySample[];
    coverage?: CoverageRecord[];
  };
}

interface MetricLike {
  value?: number | null;
  availability: string;
  basis: string;
  confidence: string;
}
export interface AnalysisLike {
  projections: { turns: { turn_id: string; metrics: Record<string, MetricLike> }[] };
}

interface ExplainedMeasurement {
  name: string;
  value: boolean | number;
  unit: string;
  evidence?: Evidence | null;
}
interface ExplainedOperation {
  operation_id?: string;
  operation_name: string;
  status: string;
  shape: "point" | "interval";
  time_basis: "monotonic" | "source_wall" | "observed_wall";
  clock_domain_id?: string | null;
  start_nano: string;
  duration_nano?: string | null;
  provider?: string | null;
  model?: string | null;
  evidence?: Evidence | null;
  measurements: ExplainedMeasurement[];
}
interface ExplainedEvent {
  event_name: string;
  time_basis: "monotonic" | "source_wall" | "observed_wall";
  clock_domain_id?: string | null;
  at_nano: string;
  participant_id?: string | null;
  evidence?: Evidence | null;
}
interface ExplainedTurn {
  turn_id: string;
  operations: ExplainedOperation[];
  events: ExplainedEvent[];
  metrics: Record<string, MetricLike>;
}
export interface ExplanationLike {
  bundle_id: string;
  session_id: string;
  session_status: string;
  finality: string;
  completeness: string;
  analyzer_version: string;
  turns: ExplainedTurn[];
  coverage: {
    signal: string;
    availability: string;
    reason?: string | null;
  }[];
  omissions: {
    omission_id: string;
    capture_class: string;
    reason: string;
    count?: number | null;
  }[];
  limitations: string[];
}

export interface MetricView {
  value: number | null;
  availability: string;
  basis: string;
  confidence: string;
}
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
        return (left.operation_id ?? left.operation_name).localeCompare(
          right.operation_id ?? right.operation_name,
        );
      })
    : candidates;
  // A single origin would falsely place independent clocks on one axis. If any
  // operation is unaligned, leave the whole turn unplaced instead of making
  // whichever operation happened to arrive first look like +0 ms.
  const origin = comparable ? (ops[0] ?? null) : null;

  return ops.map((op, index) => {
    const role = classifyRole(op.operation_name);
    const operationId = op.operation_id ?? `${op.operation_name}-${index}`;
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
  return turn.events.some(
    (event) => event.event_name === "earshot.interruption.accepted",
  );
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
  startMs: number | null;
  endMs: number | null;
  leadMs: number | null;
  timing: "interval" | "point" | "unavailable";
  evidence?: EvidenceView;
  measurements: MeasurementView[];
}
export interface EventView {
  name: string;
  atMs: number | null;
  participant: string;
  confidence: string;
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

export function buildTurnDetails(explanation: ExplanationLike): TurnDetail[] {
  return explanation.turns.map((t, index) => {
    const windows = computeOperations(t);
    const origin = windows[0]?.origin ?? null;
    return {
      turnId: t.turn_id,
      index,
      interrupted: isInterrupted(t),
      hasCascade: hasCascadeChain(windows),
      firstTokenMs: t.metrics.first_token_latency?.value ?? null,
      stages: windows.map((w) => ({
        operationId: w.operationId,
        name: w.name,
        role: w.role,
        provider: w.provider,
        model: w.model,
        status: typeof w.op.status === "string" ? w.op.status : "unknown",
        startMs: w.startMs,
        endMs: w.endMs,
        leadMs: w.leadMs,
        timing: w.timing,
        evidence: evidenceView(w.op.evidence),
        // Every measurement is carried with its real unit; booleans and other
        // non-duration domains are formatted by their unit at the view layer.
        measurements: w.op.measurements
          .filter(
            (measurement): measurement is ExplainedMeasurement =>
              typeof measurement.value === "number" ||
              typeof measurement.value === "boolean",
          )
          .map((measurement) => ({
            name: measurement.name,
            value: measurement.value,
            unit: measurement.unit,
            confidence: measurement.evidence?.confidence ?? "unavailable",
          })),
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
      })),
    };
  });
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
