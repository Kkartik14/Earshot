// Pure view-model helpers for the incident comparison surface. The browser never
// decides what changed between two incidents — the backend's `compare_incidents`
// does. These helpers only classify the backend's answer for honest rendering:
// which side failed, whether the diff is genuinely empty, and why a metric pair
// could not be subtracted.

import { ApiError } from "../../api/client";
import type { components } from "../../api/schema";

export type ComparisonResult = components["schemas"]["IncidentComparisonResponse"];
export type MetricDelta = components["schemas"]["TurnMetricDeltaResponse"];
export type AvailabilityChange =
  components["schemas"]["TurnMetricAvailabilityChangeResponse"];
export type ComparedDiagnosis = components["schemas"]["ComparedDiagnosisResponse"];
export type CoverageGap = components["schemas"]["CoverageGapResponse"];

const humanize = (value: string) => value.replace(/_/g, " ");

/** An explicit, honest rendering of a comparison that could not be produced. The
 * baseline (known-good) side keeps its own stable codes, so the reader is told
 * which incident is missing, purged, or unanalysed; a stale-analysis conflict
 * reads as exactly that. Never a blank diff and never a fabricated "no change". */
export interface ComparisonUnavailable {
  code: string;
  title: string;
  detail: string;
}

const COMPARISON_ERRORS: Record<string, { title: string; detail: string }> = {
  EARSHOT_KNOWN_GOOD_NOT_FOUND: {
    title: "Known-good incident not found",
    detail:
      "The chosen baseline is not in this project's store, so there is nothing to compare against.",
  },
  EARSHOT_KNOWN_GOOD_PURGED: {
    title: "Known-good incident was purged",
    detail:
      "The chosen baseline was deleted, so its evidence is gone and no diff can be computed against it.",
  },
  EARSHOT_KNOWN_GOOD_ANALYSIS_NOT_AVAILABLE: {
    title: "Known-good has no analysis",
    detail:
      "The baseline exists but carries no derived analysis, so there is nothing on its side to diff.",
  },
  EARSHOT_ANALYSIS_NOT_AVAILABLE: {
    title: "This incident has no analysis",
    detail:
      "This incident has no derived analysis yet, so it cannot be compared against a baseline.",
  },
  EARSHOT_ANALYSIS_BINDING_MISMATCH: {
    title: "Stale analysis — comparison withheld",
    detail:
      "A stored analysis is not derived from its incident's current evidence, so diffing the two would compare mismatched inputs. The diff is withheld until the analysis is regenerated.",
  },
};

/** Classify a failed comparison into an explicit state. A recognised backend
 * code carries its own copy; anything else (an unreachable or silent backend) is
 * reported as exactly that rather than guessed at. */
export function comparisonUnavailable(error: unknown): ComparisonUnavailable {
  const code = error instanceof ApiError ? error.message : null;
  const known = code == null ? undefined : COMPARISON_ERRORS[code];
  if (code != null && known != null) return { code, ...known };
  return {
    code: code ?? "unavailable",
    title: "Comparison unavailable",
    detail:
      code == null
        ? "The backend did not answer, so no diff is available for this pair."
        : "The backend refused this comparison. Its verdict on the two incidents is unknown, not that they are identical.",
  };
}

/** Whether a resolved comparison found anything at all to report. A genuinely
 * empty diff is a real finding ("nothing changed relative to the baseline"),
 * rendered as such — never as a blank panel. */
export function comparisonIsEmpty(result: ComparisonResult): boolean {
  return (
    result.diagnoses_added.length === 0 &&
    result.diagnoses_removed.length === 0 &&
    result.turn_metric_deltas.length === 0 &&
    result.turn_metric_availability_changes.length === 0 &&
    result.coverage_gaps_new.length === 0 &&
    result.coverage_gaps_removed.length === 0 &&
    result.contradictions_new.length === 0 &&
    result.unmatched_turns.only_in_incident.length === 0 &&
    result.unmatched_turns.only_in_known_good.length === 0
  );
}

/** Plain-language reason a metric pair is reported as a change rather than a
 * number. When the backend marked the pair `comparable: false`, the two metrics
 * are not the same quantity (different unit or measurement basis) and
 * subtracting them would fabricate a regression; when `comparable` is true, the
 * availability itself changed. Either way the two availabilities are stated. */
export function availabilityChangeReason(change: AvailabilityChange): string {
  const from = humanize(change.known_good_availability);
  const to = humanize(change.incident_availability);
  if (!change.comparable) {
    if (change.known_good_availability === change.incident_availability) {
      return `incomparable — both ${to}, but measured from different reference points, so subtracting them would invent a change`;
    }
    return `incomparable — known-good ${from}, this incident ${to}; not the same quantity, so no delta is computed`;
  }
  return `availability changed — known-good ${from}, this incident ${to}`;
}
