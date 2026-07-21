// Reconstructs a per-turn waterfall from a stored incident + its derived
// analysis. Stage operations are points on a session-relative clock; a stage's
// visible window runs to the next stage's start (and the provider-measured
// latency — ttfb/ttft — is the bright "lead" portion). `computeStages` holds
// that timing logic once; the timeline and the drawer both build on it.

export type StageName = "stt" | "llm" | "tts";
const STAGES: readonly StageName[] = ["stt", "llm", "tts"];
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
  operation_name: string;
  turn_id?: string | null;
  started_at: { monotonic_time_nano?: string | null };
  attributes?: Record<string, unknown> | null;
  evidence?: Evidence | null;
  status?: string | null;
}
interface QualitySample {
  measurements: { name: string; value: number; unit: string }[];
  attributes?: Record<string, unknown> | null;
  evidence?: { confidence?: string | null } | null;
}
interface EventRecord {
  event_name: string;
  turn_id?: string | null;
  time?: { monotonic_time_nano?: string | null };
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
      started_at?: { monotonic_time_nano?: string | null };
      ended_at?: { monotonic_time_nano?: string | null } | null;
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

export interface MetricView {
  value: number | null;
  availability: string;
  basis: string;
  confidence: string;
}
export interface StageBar {
  name: StageName;
  provider?: string;
  model?: string;
  startMs: number;
  endMs: number;
  leadMs: number;
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
  totalMs: number;
}
export interface Timeline {
  turns: TurnView[];
  scaleMs: number;
}

const nano = (value: string | null | undefined): number =>
  value == null ? 0 : Number(value) / 1_000_000;

const view = (m: MetricLike | undefined): MetricView => ({
  value: m?.value ?? null,
  availability: m?.availability ?? "not_observed",
  basis: m?.basis ?? "",
  confidence: m?.confidence ?? "unavailable",
});

const roundUp = (value: number, step: number): number =>
  Math.max(step, Math.ceil(value / step) * step);

const attr = (op: Operation, key: string): string | undefined =>
  typeof op.attributes?.[key] === "string" ? (op.attributes[key] as string) : undefined;

interface StageWindow {
  op: Operation;
  name: StageName;
  provider?: string;
  model?: string;
  startMs: number;
  endMs: number;
  leadMs: number;
  confidence?: string;
}

/** Shared per-turn stage windows: the timing logic both views build on. */
function computeStages(incident: IncidentLike, turnId: string): StageWindow[] {
  const { operations, quality_samples } = incident.profile;
  const sample = (name: string) => {
    for (const s of quality_samples) {
      if (s.attributes?.["earshot.turn.id"] !== turnId) continue;
      const m = s.measurements.find((x) => x.name === name);
      if (m) return { value: m.value, confidence: s.evidence?.confidence ?? undefined };
    }
    return undefined;
  };

  const stageOps = operations
    .filter((o) => o.turn_id === turnId && STAGES.includes(o.operation_name as StageName))
    .sort(
      (a, b) =>
        nano(a.started_at.monotonic_time_nano) - nano(b.started_at.monotonic_time_nano),
    );
  const turnStart = stageOps.length
    ? nano(stageOps[0].started_at.monotonic_time_nano)
    : 0;

  return stageOps.map((op, i) => {
    const name = op.operation_name as StageName;
    const startMs = nano(op.started_at.monotonic_time_nano) - turnStart;
    const lead = sample(LEAD_METRIC[name]);
    const next = stageOps[i + 1];
    const endMs =
      next != null
        ? nano(next.started_at.monotonic_time_nano) - turnStart
        : startMs + (lead?.value ?? 0);
    return {
      op,
      name,
      provider: attr(op, "gen_ai.provider.name"),
      model: attr(op, "gen_ai.request.model"),
      startMs,
      endMs,
      leadMs: lead?.value ?? endMs - startMs,
      confidence: lead?.confidence,
    };
  });
}

function isInterrupted(incident: IncidentLike, turnId: string): boolean {
  return incident.profile.events.some(
    (e) => e.turn_id === turnId && e.event_name === "earshot.interruption.accepted",
  );
}

export function buildTimeline(incident: IncidentLike, analysis: AnalysisLike): Timeline {
  const turns: TurnView[] = analysis.projections.turns.map((t, index) => {
    const windows = computeStages(incident, t.turn_id);
    const stages: StageBar[] = windows.map((w) => ({
      name: w.name,
      provider: w.provider,
      model: w.model,
      startMs: w.startMs,
      endMs: w.endMs,
      leadMs: w.leadMs,
      confidence: w.confidence,
    }));
    return {
      turnId: t.turn_id,
      index,
      stages,
      firstToken: view(t.metrics.first_token_latency),
      generated: view(t.metrics.generated_response_latency),
      response: view(t.metrics.response_latency),
      interrupted: isInterrupted(incident, t.turn_id),
      totalMs: stages.reduce((max, s) => Math.max(max, s.endMs), 0),
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
  value: number;
  unit: string;
  confidence: string;
}
export interface StageDetail {
  name: StageName;
  provider?: string;
  model?: string;
  status: string;
  startMs: number;
  endMs: number;
  leadMs: number;
  evidence?: EvidenceView;
  measurements: MeasurementView[];
}
export interface EventView {
  name: string;
  atMs: number;
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

export function buildTurnDetails(
  incident: IncidentLike,
  analysis: AnalysisLike,
): TurnDetail[] {
  const { events, quality_samples } = incident.profile;

  const stageMeasurements = (turnId: string, name: StageName): MeasurementView[] => {
    const out: MeasurementView[] = [];
    for (const s of quality_samples) {
      if (s.attributes?.["earshot.turn.id"] !== turnId) continue;
      for (const m of s.measurements) {
        if (m.name.includes(name)) {
          out.push({
            name: m.name,
            value: m.value,
            unit: m.unit,
            confidence: s.evidence?.confidence ?? "unavailable",
          });
        }
      }
    }
    return out;
  };

  return analysis.projections.turns.map((t, index) => {
    const windows = computeStages(incident, t.turn_id);
    const turnStart = windows.length
      ? nano(windows[0].op.started_at.monotonic_time_nano)
      : 0;
    return {
      turnId: t.turn_id,
      index,
      interrupted: isInterrupted(incident, t.turn_id),
      firstTokenMs: t.metrics.first_token_latency?.value ?? null,
      stages: windows.map((w) => ({
        name: w.name,
        provider: w.provider,
        model: w.model,
        status: typeof w.op.status === "string" ? w.op.status : "unknown",
        startMs: w.startMs,
        endMs: w.endMs,
        leadMs: w.leadMs,
        evidence: evidenceView(w.op.evidence),
        measurements: stageMeasurements(t.turn_id, w.name),
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
      events: events
        .filter((e) => e.turn_id === t.turn_id)
        .map((e) => ({
          name: e.event_name,
          atMs: nano(e.time?.monotonic_time_nano) - turnStart,
          participant: (e.participant_id ?? "").split("-").pop() ?? "",
          confidence: e.evidence?.confidence ?? "",
        })),
    };
  });
}

export function getCoverage(incident: IncidentLike): CoverageRow[] {
  return (incident.profile.coverage ?? [])
    .filter((c) => c.availability !== "available")
    .map((c) => ({
      signal: c.signal,
      availability: c.availability,
      reason: c.reason ?? undefined,
    }));
}

export interface SessionSummary {
  sessionId: string;
  status: string;
  stack: string[];
  turns: number;
  durationMs: number;
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

export function buildSummary(incident: IncidentLike, timeline: Timeline): SessionSummary {
  const stack: string[] = [];
  const seen = new Set<string>();
  for (const turn of timeline.turns) {
    for (const stage of turn.stages) {
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
  const started = nano(incident.profile.session?.started_at?.monotonic_time_nano);
  const ended = nano(incident.profile.session?.ended_at?.monotonic_time_nano);

  return {
    sessionId: incident.profile.manifest?.session_id ?? "session",
    status: incident.profile.session?.status ?? "unknown",
    stack,
    turns: timeline.turns.length,
    durationMs: Math.max(0, ended - started),
    p95FirstTokenMs: percentile(firstTokens, 95),
    interruptions: timeline.turns.filter((t) => t.interrupted).length,
  };
}
