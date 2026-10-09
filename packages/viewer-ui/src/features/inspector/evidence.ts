// Pure view-model helpers for the "what the evidence does not tell us" surface.
// The browser never decides what is missing — the backend's `EvidenceQuery`
// does, through `/v1/incidents/{id}/evidence/not_observed`. These helpers only
// classify the backend's answer for honest rendering: whether the projection
// could be produced, and whether it examined the incident and found no gaps
// (which is a real finding, never a blank).

import { ApiError } from "../../api/client";
import type { components } from "../../api/schema";

export type NotObserved = components["schemas"]["NotObservedResponse"];
export type EvidenceSummary = components["schemas"]["EvidenceSummaryResponse"];
export type CoverageGap = components["schemas"]["CoverageGapResponse"];
export type AnalysisLimitation = components["schemas"]["AnalysisLimitationResponse"];
export type TurnMetricLimitation = components["schemas"]["TurnMetricLimitationResponse"];
export type EvidenceLimitation = AnalysisLimitation | TurnMetricLimitation;
export type EvidenceOmission = components["schemas"]["EvidenceOmissionResponse"];

/** An explicit, honest rendering of a not-observed projection that could not be
 * produced. A missing analysis and a stale (foreign) one each keep their own
 * stable backend code, so the reader is told the projection was withheld and
 * why — never shown an empty "nothing is missing" that would read as a clean
 * bill of health. */
export interface NotObservedUnavailable {
  code: string;
  title: string;
  detail: string;
}

const NOT_OBSERVED_ERRORS: Record<string, { title: string; detail: string }> = {
  EARSHOT_ANALYSIS_NOT_AVAILABLE: {
    title: "No analysis — coverage unknown",
    detail:
      "This incident carries no derived analysis, so the projection of what the evidence does not observe could not be computed. What is missing is unknown, not nothing.",
  },
  EARSHOT_ANALYSIS_BINDING_MISMATCH: {
    title: "Stale analysis — coverage withheld",
    detail:
      "A stored analysis is not derived from this incident's current evidence, so reporting its gaps would attribute another artifact's blind spots to this one. The projection is withheld until the analysis is regenerated.",
  },
};

/** Classify a failed not-observed query into an explicit state. A recognised
 * backend code carries its own copy; anything else (an unreachable or silent
 * backend) is reported as exactly that rather than guessed at. */
export function notObservedUnavailable(error: unknown): NotObservedUnavailable {
  const code = error instanceof ApiError ? error.message : null;
  const known = code == null ? undefined : NOT_OBSERVED_ERRORS[code];
  if (code != null && known != null) return { code, ...known };
  return {
    code: code ?? "unavailable",
    title: "Coverage unavailable",
    detail:
      code == null
        ? "The backend did not answer, so it is unknown what this incident's evidence does not observe."
        : "The backend refused this projection, so its account of the incident's blind spots is unknown, not that there are none.",
  };
}

/** Whether a resolved projection examined the incident and found no gaps,
 * limitations, or omissions. That is a real finding — the evidence graph reports
 * full coverage — rendered as such, never as a blank panel. */
export function notObservedIsEmpty(result: NotObserved): boolean {
  return (
    result.coverage_gaps.length === 0 &&
    result.limitations.length === 0 &&
    result.omissions.length === 0
  );
}
