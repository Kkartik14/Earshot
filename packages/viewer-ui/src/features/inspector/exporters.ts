// Pure helpers for the export surface. The list of exporter names mirrors the
// spec's `format` enum, and the refusal classifier turns the backend's stable
// export codes into explicit, honest states — never a crash or an empty result.

import { ApiError } from "../../api/client";
import type { ExportFormat } from "../../api/hooks";

/** The exporter names the viewer offers to project an incident through. Annotated
 * `readonly ExportFormat[]`, so a spec change that drops one of these turns the
 * stale entry into a build error here rather than a request that 400s. The value
 * itself comes from the endpoint/spec — this list only mirrors it. */
export const EXPORT_FORMATS: readonly ExportFormat[] = ["otlp", "openinference"];

/** An explicit, honest rendering of an export the backend refused or could not
 * produce. A policy denial and an unknown exporter each keep their own stable
 * code so the refusal names its own cause, never a blank document. */
export interface ExportRefusal {
  code: string;
  title: string;
  detail: string;
}

const EXPORT_ERRORS: Record<string, { title: string; detail: string }> = {
  EARSHOT_EXPORT_DENIED: {
    title: "Export denied by policy",
    detail:
      "This incident's capture policy does not permit projecting it to this destination, so the exporter was refused. No document was produced — a governance decision, not an empty result.",
  },
  EARSHOT_UNKNOWN_EXPORT_FORMAT: {
    title: "Unknown exporter",
    detail:
      "No exporter is registered under this name, so there is nothing to project this incident through.",
  },
};

/** Classify a failed export into an explicit refusal. A recognised backend code
 * carries its own copy; anything else (an unreachable or silent backend) is
 * reported as exactly that rather than guessed at. */
export function exportRefusal(error: unknown): ExportRefusal {
  const code = error instanceof ApiError ? error.message : null;
  const known = code == null ? undefined : EXPORT_ERRORS[code];
  if (code != null && known != null) return { code, ...known };
  return {
    code: code ?? "unavailable",
    title: "Export unavailable",
    detail:
      code == null
        ? "The backend did not answer, so no projection was produced."
        : "The backend refused this export, and no document was produced.",
  };
}
