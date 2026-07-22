import type { components } from "../../api/schema";

export type MetricGroup = components["schemas"]["TurnMetricGroupResponse"];
export type MetricSummary = components["schemas"]["TurnMetricSummaryResponse"];

/** The latency metrics worth charting across a fleet, in display order. */
export const METRICS = [
  { key: "first_token_ms", label: "First token", budgetMs: 500 },
  { key: "response_ms", label: "Response" },
  { key: "turn_duration_ms", label: "Turn duration" },
  { key: "generated_response_ms", label: "Generated response" },
  { key: "stt_finalization_ms", label: "STT finalization" },
  { key: "eou_ms", label: "End of utterance" },
] as const;
export type MetricKey = (typeof METRICS)[number]["key"];

export const GROUP_BYS = [
  { key: "model", label: "Model" },
  { key: "provider", label: "Provider" },
  { key: "framework", label: "Framework" },
  { key: "status", label: "Status" },
  { key: "language", label: "Language" },
] as const;
export type GroupBy = (typeof GROUP_BYS)[number]["key"];

export const budgetFor = (metric: MetricKey): number | null => {
  const found = METRICS.find((m) => m.key === metric);
  return found && "budgetMs" in found ? found.budgetMs : null;
};

export interface FleetSummary {
  turns: number;
  measured: number;
  coveragePct: number | null;
  worstP95: number | null;
  bestP50: number | null;
}

/** Roll the per-group rows up into the headline strip. Percentiles can't be
 * averaged across groups, so we report the worst p95 / best p50 seen, not a
 * fabricated overall percentile. */
export function summarizeGroups(groups: MetricGroup[]): FleetSummary {
  const turns = groups.reduce((n, g) => n + g.turn_count, 0);
  const measured = groups.reduce((n, g) => n + g.available_count, 0);
  const p95s = groups.map((g) => g.p95_ms).filter((v): v is number => v != null);
  const p50s = groups.map((g) => g.p50_ms).filter((v): v is number => v != null);
  return {
    turns,
    measured,
    coveragePct: turns > 0 ? (measured / turns) * 100 : null,
    worstP95: p95s.length ? Math.max(...p95s) : null,
    bestP50: p50s.length ? Math.min(...p50s) : null,
  };
}

/** Groups ranked slowest-p95 first; groups with no measurement sink to the end. */
export function rankByP95(groups: MetricGroup[]): MetricGroup[] {
  return [...groups].sort((a, b) => (b.p95_ms ?? -1) - (a.p95_ms ?? -1));
}
