import { ApiError } from "../../api/client";
import type { components } from "../../api/schema";
import { formatRelativeTime } from "../../lib/format";
import styles from "./AnalysisSummary.module.css";

type StoredAnalysis = components["schemas"]["StoredAnalysisResponse"];

/** Whether the analyzer's own output could be read for this incident. `pending`
 * and `unavailable` are distinct on purpose: only `ready` carries a binding. */
export type AnalysisStatus = "pending" | "unavailable" | "ready";

/** The analyzer's own output for one incident, as the `/analysis` endpoint
 * returns it: the identity and evidence binding that pins every derived reading,
 * plus the counts of what it produced. Turn count is `null` when the analyzer
 * attached no projection — unknown, never rendered as zero. */
export interface AnalysisSummaryView {
  analyzerName: string;
  analyzerVersion: string;
  inputDigest: string;
  generatedAtUnixNano: string;
  diagnosisCount: number;
  turnCount: number | null;
}

/** Why the analyzer's output has no answer. A backend refusal carries its own
 * stable code (the useful thing to show); anything else is reported as exactly
 * that rather than guessed at. */
export function analysisReason(error: unknown): string {
  return error instanceof ApiError ? error.message : "the backend did not answer";
}

/** Project the stored analysis response into the summary view. Reads only fields
 * the `/analysis` endpoint returns; it invents nothing the analyzer did not. */
export function buildAnalysisSummary(data: StoredAnalysis): AnalysisSummaryView {
  return {
    analyzerName: data.analysis.analyzer_name,
    analyzerVersion: data.analyzer_version,
    inputDigest: data.input_digest,
    generatedAtUnixNano: data.generated_at_unix_nano,
    diagnosisCount: data.analysis.diagnoses.length,
    turnCount: data.analysis.projections?.turns.length ?? null,
  };
}

/** The analyzer's own output for this incident: which analyzer ran, the exact
 * evidence digest it is bound to, when it ran, and how much it produced. This is
 * the provenance the rest of the inspector's readings rest on; surfaced so the
 * inspector reflects the analyzer's actual output rather than an anonymous
 * projection of it. */
export function AnalysisSummaryPanel({
  status,
  reason,
  view,
}: {
  status: AnalysisStatus;
  /** Why the analysis could not be read. Required reading when `unavailable`. */
  reason: string | null;
  view: AnalysisSummaryView | null;
}) {
  return (
    <section className={styles.panel} aria-label="Analysis">
      <div className={styles.panelHead}>
        <h2>Analysis</h2>
        <span className={styles.note}>
          {status === "ready"
            ? "the analyzer's output, bound to the evidence it read"
            : ""}
        </span>
      </div>

      {status === "unavailable" ? (
        <p className={styles.limit} role="status">
          This incident has no derived analysis
          {reason == null ? "" : ` · ${reason}`}. The analyzer's readings are unavailable,
          not empty.
        </p>
      ) : status === "pending" ? (
        <p className={styles.limit}>Reading the analyzer&rsquo;s output…</p>
      ) : view == null ? null : (
        <div className={styles.rows}>
          <div className={styles.row}>
            <span className={styles.label}>analyzer</span>
            <span className={styles.value}>
              {view.analyzerName} · {view.analyzerVersion}
            </span>
          </div>
          <div className={styles.row}>
            <span className={styles.label}>evidence digest</span>
            <span className={styles.value}>
              {view.inputDigest.length > 20
                ? `${view.inputDigest.slice(0, 20)}…`
                : view.inputDigest}
            </span>
          </div>
          <div className={styles.row}>
            <span className={styles.label}>generated</span>
            <span className={styles.value}>
              {formatRelativeTime(view.generatedAtUnixNano)}
            </span>
          </div>
          <div className={styles.row}>
            <span className={styles.label}>diagnoses raised</span>
            <span className={styles.value}>{view.diagnosisCount}</span>
          </div>
          <div className={styles.row}>
            <span className={styles.label}>turns analyzed</span>
            <span
              className={
                view.turnCount == null ? `${styles.value} ${styles.dim}` : styles.value
              }
            >
              {view.turnCount == null ? "not projected" : view.turnCount}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
