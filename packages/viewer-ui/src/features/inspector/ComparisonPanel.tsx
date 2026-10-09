import { useState } from "react";
import { useComparison, useIncidents } from "../../api/hooks";
import { formatMeasurement } from "../../lib/format";
import {
  availabilityChangeReason,
  comparisonIsEmpty,
  comparisonUnavailable,
  type ComparedDiagnosis,
  type ComparisonResult,
  type CoverageGap,
} from "./comparison";
import styles from "./ComparisonPanel.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");
const shortId = (value: string) => (value.length > 20 ? `${value.slice(0, 20)}…` : value);
const shortDigest = (value: string) =>
  value.length > 12 ? `${value.slice(0, 12)}…` : value;

const signedMeasurement = (value: number, unit: string): string => {
  const rendered = formatMeasurement(value, unit);
  return value > 0 ? `+${rendered}` : rendered;
};

function IdChips({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <div className={styles.chips}>
      {ids.map((id) => (
        <span key={id} className={styles.chip}>
          {id}
        </span>
      ))}
    </div>
  );
}

function DiagnosisRows({ diagnoses }: { diagnoses: ComparedDiagnosis[] }) {
  return (
    <div className={styles.list}>
      {diagnoses.map((d) => (
        <article key={d.diagnosis_id} className={styles.card}>
          <div className={styles.cardHead}>
            <span className={styles.code}>{d.code}</span>
            <span className={styles.boundary}>{d.boundary}</span>
          </div>
          <IdChips ids={d.turn_ids} />
        </article>
      ))}
    </div>
  );
}

function CoverageRows({ gaps }: { gaps: CoverageGap[] }) {
  return (
    <div className={styles.measBlock}>
      {gaps.map((gap) => (
        <div key={`${gap.signal}:${gap.availability}`} className={styles.measRow}>
          <span className={styles.measName}>{gap.signal}</span>
          <span className={`${styles.measVal} ${styles.dim}`}>
            {humanize(gap.availability)}
          </span>
          <span className={styles.reason}>
            {gap.reason == null ? "no reason declared" : humanize(gap.reason)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** The structured diff of a resolved comparison. Every branch that a viewer might
 * collapse into "no change" is rendered as its own explicit row: an
 * `comparable: false` metric pair states why it is incomparable, and a genuinely
 * empty diff says so in words. */
function ComparisonDiff({ result }: { result: ComparisonResult }) {
  if (comparisonIsEmpty(result)) {
    return (
      <p className={styles.empty}>
        Nothing changed relative to this baseline: the analyzer found no added or removed
        diagnosis, no metric change, no coverage difference, and no new contradiction
        across the turns the two incidents share.
      </p>
    );
  }
  return (
    <div className={styles.sections}>
      {result.turn_metric_deltas.length > 0 ? (
        <section className={styles.section} aria-label="Metric changes">
          <h4 className={styles.sectionHead}>Metric changes</h4>
          <div className={styles.measBlock}>
            {result.turn_metric_deltas.map((delta) => (
              <div key={`${delta.turn_id}:${delta.metric}`} className={styles.measRow}>
                <span className={styles.measName}>
                  {delta.turn_id} · {humanize(delta.metric)}
                </span>
                <span className={styles.measVal}>
                  {signedMeasurement(delta.delta, delta.unit)}
                </span>
                <span className={styles.reason}>
                  known-good {formatMeasurement(delta.known_good_value, delta.unit)} →{" "}
                  {formatMeasurement(delta.incident_value, delta.unit)}
                </span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {result.turn_metric_availability_changes.length > 0 ? (
        <section className={styles.section} aria-label="Availability changes">
          <h4 className={styles.sectionHead}>Availability changes</h4>
          <div className={styles.measBlock}>
            {result.turn_metric_availability_changes.map((change) => (
              <div key={`${change.turn_id}:${change.metric}`} className={styles.measRow}>
                <span className={styles.measName}>
                  {change.turn_id} · {humanize(change.metric)}
                </span>
                <span
                  className={
                    change.comparable
                      ? styles.availTag
                      : `${styles.availTag} ${styles.incomparable}`
                  }
                >
                  {change.comparable ? "changed" : "incomparable"}
                </span>
                <span className={styles.reason}>{availabilityChangeReason(change)}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {result.diagnoses_added.length > 0 ? (
        <section className={styles.section} aria-label="Diagnoses added">
          <h4 className={styles.sectionHead}>
            Diagnoses added{" "}
            <span className={styles.count}>{result.diagnoses_added.length}</span>
          </h4>
          <DiagnosisRows diagnoses={result.diagnoses_added} />
        </section>
      ) : null}

      {result.diagnoses_removed.length > 0 ? (
        <section className={styles.section} aria-label="Diagnoses resolved">
          <h4 className={styles.sectionHead}>
            Diagnoses resolved{" "}
            <span className={styles.count}>{result.diagnoses_removed.length}</span>
          </h4>
          <DiagnosisRows diagnoses={result.diagnoses_removed} />
        </section>
      ) : null}

      {result.contradictions_new.length > 0 ? (
        <section className={styles.section} aria-label="New contradictions">
          <h4 className={styles.sectionHead}>
            New contradictions{" "}
            <span className={styles.count}>{result.contradictions_new.length}</span>
          </h4>
          <div className={styles.list}>
            {result.contradictions_new.map((c, index) => (
              <article
                key={`${c.kind}:${c.subject ?? ""}:${index}`}
                className={styles.card}
              >
                <div className={styles.cardHead}>
                  <span className={styles.code}>{humanize(c.kind)}</span>
                  {c.boundary != null ? (
                    <span className={styles.boundary}>{c.boundary}</span>
                  ) : null}
                </div>
                <p className={styles.summary}>{humanize(c.summary)}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {result.coverage_gaps_new.length > 0 ? (
        <section className={styles.section} aria-label="Coverage gaps introduced">
          <h4 className={styles.sectionHead}>Coverage gaps introduced</h4>
          <CoverageRows gaps={result.coverage_gaps_new} />
        </section>
      ) : null}

      {result.coverage_gaps_removed.length > 0 ? (
        <section className={styles.section} aria-label="Coverage gaps closed">
          <h4 className={styles.sectionHead}>Coverage gaps closed</h4>
          <CoverageRows gaps={result.coverage_gaps_removed} />
        </section>
      ) : null}

      {result.unmatched_turns.only_in_incident.length > 0 ||
      result.unmatched_turns.only_in_known_good.length > 0 ? (
        <section className={styles.section} aria-label="Unmatched turns">
          <h4 className={styles.sectionHead}>Unmatched turns</h4>
          {result.unmatched_turns.only_in_incident.length > 0 ? (
            <div className={styles.unmatched}>
              <span className={styles.unmatchedLabel}>only in this incident</span>
              <IdChips ids={result.unmatched_turns.only_in_incident} />
            </div>
          ) : null}
          {result.unmatched_turns.only_in_known_good.length > 0 ? (
            <div className={styles.unmatched}>
              <span className={styles.unmatchedLabel}>only in the known-good</span>
              <IdChips ids={result.unmatched_turns.only_in_known_good} />
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

/** Pick a known-good incident and diff this one against it.
 *
 * The picker is loaded lazily — the session list is only fetched once the reader
 * opens it. Every failure the backend can report about the baseline
 * (`EARSHOT_KNOWN_GOOD_NOT_FOUND` / `_PURGED` / `_ANALYSIS_NOT_AVAILABLE`) or
 * about a stale analysis (`EARSHOT_ANALYSIS_BINDING_MISMATCH`) is rendered as an
 * explicit state, and a metric pair the analyzer marked incomparable is shown as
 * incomparable with its reason — never as a zero, a hidden row, or a blank. */
export function ComparisonPanel({ bundleId }: { bundleId: string | undefined }) {
  const [open, setOpen] = useState(false);
  const [knownGood, setKnownGood] = useState<string | null>(null);
  const incidents = useIncidents({ limit: 50 }, { enabled: open });
  const comparison = useComparison(bundleId, knownGood);

  const options = (incidents.data?.items ?? []).filter(
    (item) => item.bundle_id !== bundleId,
  );

  return (
    <section className={styles.panel} aria-label="Compare against a known-good session">
      <div className={styles.panelHead}>
        <h2>Compare</h2>
        <span className={styles.note}>
          diff this incident against a known-good session
        </span>
      </div>

      <button
        type="button"
        className={styles.disclosure}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "Hide baseline picker" : "Compare against a known-good session"}
      </button>

      {open ? (
        <div className={styles.picker}>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>Known-good session</span>
            <select
              className={styles.select}
              value={knownGood ?? ""}
              onChange={(event) => setKnownGood(event.target.value || null)}
            >
              <option value="">Select a baseline…</option>
              {options.map((item) => (
                <option key={item.bundle_id} value={item.bundle_id}>
                  {item.session_id} · {item.framework ?? "custom"}
                  {item.finality === "final" ? "" : ` · ${item.finality}`}
                </option>
              ))}
            </select>
          </label>

          {incidents.isPending ? (
            <p className={styles.hint}>Loading the session list…</p>
          ) : incidents.isError ? (
            <p className={styles.hint}>
              The session list could not be loaded, so there is nothing to pick a baseline
              from.
            </p>
          ) : options.length === 0 ? (
            <p className={styles.hint}>
              No other stored session is available to compare this incident against.
            </p>
          ) : null}
        </div>
      ) : null}

      {knownGood != null ? (
        comparison.isPending ? (
          <p className={styles.hint}>Comparing against the chosen baseline…</p>
        ) : comparison.data ? (
          <div className={styles.result}>
            <p className={styles.provenance}>
              this incident <code>{shortId(comparison.data.bundle_id)}</code> (
              {shortDigest(comparison.data.input_digest)}) vs known-good{" "}
              <code>{shortId(comparison.data.known_good_bundle_id)}</code> (
              {shortDigest(comparison.data.known_good_input_digest)}) · analyzer{" "}
              {comparison.data.analyzer_version}
            </p>
            <ComparisonDiff result={comparison.data} />
          </div>
        ) : (
          (() => {
            const state = comparisonUnavailable(comparison.error);
            return (
              <div className={styles.unavailable} role="status">
                <div className={styles.unavailableHead}>
                  <span className={styles.unavailableTitle}>{state.title}</span>
                  <span className={styles.code}>{state.code}</span>
                </div>
                <p className={styles.unavailableDetail}>{state.detail}</p>
              </div>
            );
          })()
        )
      ) : null}
    </section>
  );
}
