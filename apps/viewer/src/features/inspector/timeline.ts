// Reconstructs a per-turn waterfall from a stored incident + its derived
// analysis. Stage operations are points on a session-relative clock; a stage's
// visible window runs to the next stage's start (and the provider-measured
// latency — ttfb/ttft — is the bright "lead" portion).

export type StageName = "stt" | "llm" | "tts";
const STAGES: readonly StageName[] = ["stt", "llm", "tts"];
const LEAD_METRIC: Record<StageName, string> = {
  stt: "earshot.stt.ttfb",
  llm: "earshot.llm.ttft",
  tts: "earshot.tts.ttfb",
};

interface Operation {
  operation_name: string;
  turn_id?: string | null;
  started_at: { monotonic_time_nano?: string | null };
  attributes?: Record<string, unknown> | null;
}
interface QualitySample {
  measurements: { name: string; value: number; unit: string }[];
  attributes?: Record<string, unknown> | null;
  evidence?: { confidence?: string | null } | null;
}
interface EventRecord {
  event_name: string;
  turn_id?: string | null;
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

export interface StageBar {
  name: StageName;
  provider?: string;
  model?: string;
  startMs: number;
  endMs: number;
  leadMs: number;
  confidence?: string;
}
export interface MetricView {
  value: number | null;
  availability: string;
  basis: string;
  confidence: string;
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

export function buildTimeline(incident: IncidentLike, analysis: AnalysisLike): Timeline {
  const { operations, events, quality_samples } = incident.profile;

  const sample = (turnId: string, name: string) => {
    for (const s of quality_samples) {
      if (s.attributes?.["earshot.turn.id"] !== turnId) continue;
      const m = s.measurements.find((x) => x.name === name);
      if (m) return { value: m.value, confidence: s.evidence?.confidence ?? undefined };
    }
    return undefined;
  };

  const turns: TurnView[] = analysis.projections.turns.map((t, index) => {
    const stageOps = operations
      .filter(
        (o) => o.turn_id === t.turn_id && STAGES.includes(o.operation_name as StageName),
      )
      .sort(
        (a, b) =>
          nano(a.started_at.monotonic_time_nano) - nano(b.started_at.monotonic_time_nano),
      );
    const turnStart = stageOps.length
      ? nano(stageOps[0].started_at.monotonic_time_nano)
      : 0;

    const stages: StageBar[] = stageOps.map((o, i) => {
      const name = o.operation_name as StageName;
      const startMs = nano(o.started_at.monotonic_time_nano) - turnStart;
      const lead = sample(t.turn_id, LEAD_METRIC[name]);
      const next = stageOps[i + 1];
      const endMs =
        next != null
          ? nano(next.started_at.monotonic_time_nano) - turnStart
          : startMs + (lead?.value ?? 0);
      return {
        name,
        provider: o.attributes?.["gen_ai.provider.name"] as string | undefined,
        model: o.attributes?.["gen_ai.request.model"] as string | undefined,
        startMs,
        endMs,
        leadMs: lead?.value ?? endMs - startMs,
        confidence: lead?.confidence,
      };
    });

    return {
      turnId: t.turn_id,
      index,
      stages,
      firstToken: view(t.metrics.first_token_latency),
      generated: view(t.metrics.generated_response_latency),
      response: view(t.metrics.response_latency),
      interrupted: events.some(
        (e) =>
          e.turn_id === t.turn_id && e.event_name === "earshot.interruption.accepted",
      ),
      totalMs: stages.reduce((max, s) => Math.max(max, s.endMs), 0),
    };
  });

  return { turns, scaleMs: roundUp(Math.max(1, ...turns.map((t) => t.totalMs)), 250) };
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
