import { useState } from "react";
import { useEvidenceSummary, useNotObserved } from "../../api/hooks";
import {
  notObservedIsEmpty,
  notObservedUnavailable,
  type CoverageGap,
  type EvidenceLimitation,
  type EvidenceOmission,
  type EvidenceSummary,
  type NotObserved,
} from "./evidence";
import styles from "./EvidencePanel.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

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

/** Signals a fact source could not fully observe. Availability and reason are
 * always stated; a gap with no declared reason says so rather than showing a
 * blank. */
function CoverageGaps({ gaps }: { gaps: CoverageGap[] }) {
  return (
    <div className={styles.list}>
      {gaps.map((gap) => (
        <article key={`${gap.signal}:${gap.availability}`} className={styles.card}>
          <div className={styles.cardHead}>
            <span className={styles.code}>{gap.signal}</span>
            <span className={styles.tag}>{humanize(gap.availability)}</span>
          </div>
          <p className={styles.reason}>
            {gap.reason == null ? "no reason declared" : humanize(gap.reason)}
          </p>
        </article>
      ))}
    </div>
  );
}

/** Limitations on what could be derived — one for the analysis as a whole, or one
 * per turn-metric that was not `available`. Each names its own reason; a
 * turn-metric limitation also cites the evidence it would have needed, so an
 * unavailable metric reads as an explicit unknown and never as a zero. */
function Limitations({ limitations }: { limitations: EvidenceLimitation[] }) {
  return (
    <div className={styles.list}>
      {limitations.map((limitation, index) =>
        limitation.scope === "analysis" ? (
          <article
            key={`analysis:${limitation.limitation}:${index}`}
            className={styles.card}
          >
            <div className={styles.cardHead}>
              <span className={styles.scope}>analysis</span>
              <span className={styles.code}>{humanize(limitation.limitation)}</span>
            </div>
          </article>
        ) : (
          <article
            key={`turn:${limitation.turn_id}:${limitation.metric}:${index}`}
            className={styles.card}
          >
            <div className={styles.cardHead}>
              <span className={styles.scope}>{limitation.turn_id}</span>
              <span className={styles.code}>{humanize(limitation.metric)}</span>
              <span className={styles.tag}>{humanize(limitation.availability)}</span>
            </div>
            <p className={styles.reason}>{humanize(limitation.limitation)}</p>
            <IdChips ids={limitation.evidence_ids} />
          </article>
        ),
      )}
    </div>
  );
}

/** Things a capture policy deliberately did not record. `count` is how many were
 * omitted when they could be counted, and "count not recorded" when the loss was
 * not countable — which is not the same claim as zero. */
function Omissions({ omissions }: { omissions: EvidenceOmission[] }) {
  return (
    <div className={styles.list}>
      {omissions.map((omission) => (
        <article key={omission.omission_id} className={styles.card}>
          <div className={styles.cardHead}>
            <span className={styles.code}>{humanize(omission.capture_class)}</span>
            <span className={styles.tag}>
              {omission.count == null
                ? "count not recorded"
                : `${omission.count} omitted`}
            </span>
          </div>
          <p className={styles.reason}>{humanize(omission.reason)}</p>
          <IdChips ids={omission.source_refs} />
        </article>
      ))}
    </div>
  );
}

/** The resolved not-observed projection. Every category is rendered as its own
 * explicit section; a genuinely empty projection is stated as an examined result,
 * never as a blank panel that would read as "nothing is missing". */
function NotObservedDetail({ result }: { result: NotObserved }) {
  if (notObservedIsEmpty(result)) {
    return (
      <p className={styles.empty}>
        The evidence graph reports no coverage gaps, no analysis or turn limitations, and
        no policy omissions for this incident. This is an examined result — the projection
        ran against this incident's analysis — not an absence of checking.
      </p>
    );
  }
  return (
    <div className={styles.sections}>
      {result.coverage_gaps.length > 0 ? (
        <section className={styles.section} aria-label="Coverage gaps">
          <h4 className={styles.sectionHead}>
            Coverage gaps{" "}
            <span className={styles.count}>{result.coverage_gaps.length}</span>
          </h4>
          <CoverageGaps gaps={result.coverage_gaps} />
        </section>
      ) : null}

      {result.limitations.length > 0 ? (
        <section className={styles.section} aria-label="Limitations">
          <h4 className={styles.sectionHead}>
            Limitations <span className={styles.count}>{result.limitations.length}</span>
          </h4>
          <Limitations limitations={result.limitations} />
        </section>
      ) : null}

      {result.omissions.length > 0 ? (
        <section className={styles.section} aria-label="Omissions">
          <h4 className={styles.sectionHead}>
            Omissions <span className={styles.count}>{result.omissions.length}</span>
          </h4>
          <Omissions omissions={result.omissions} />
        </section>
      ) : null}
    </div>
  );
}

/** A one-line, session-level frame for the unknowns, drawn from the digest. It is
 * enrichment only: when the summary is unavailable or stale this line is simply
 * omitted, and the not-observed projection alone drives the panel's coded state. */
function SummaryFrame({ summary }: { summary: EvidenceSummary }) {
  const c = summary.counts;
  return (
    <p className={styles.frame}>
      {c.turn_count} turns · {c.diagnosis_count} diagnoses · {c.coverage_gap_count}{" "}
      coverage gaps · {c.contradiction_count} contradictions
    </p>
  );
}

/** What this incident's evidence explicitly does NOT tell us: coverage gaps,
 * analysis/turn limitations, and policy omissions, each rendered as a first-class
 * unknown with its own reason — never a blank or a zero. A missing analysis
 * (`EARSHOT_ANALYSIS_NOT_AVAILABLE`) or a stale one
 * (`EARSHOT_ANALYSIS_BINDING_MISMATCH`) renders as its own coded state, so the
 * panel never collapses "unknown coverage" into "nothing is missing". */
export function EvidencePanel({ bundleId }: { bundleId: string | undefined }) {
  const [open, setOpen] = useState(true);
  const notObserved = useNotObserved(bundleId);
  const summary = useEvidenceSummary(bundleId);

  return (
    <section className={styles.panel} aria-label="What the evidence does not tell us">
      <div className={styles.panelHead}>
        <h2>Not observed</h2>
        <span className={styles.note}>
          what this incident's evidence does not tell us
        </span>
      </div>

      <button
        type="button"
        className={styles.disclosure}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "Hide what was not observed" : "Show what was not observed"}
      </button>

      {open ? (
        <div className={styles.body}>
          {summary.data ? <SummaryFrame summary={summary.data} /> : null}
          {notObserved.isPending ? (
            <p className={styles.hint}>Reading what the evidence does not observe…</p>
          ) : notObserved.data ? (
            <NotObservedDetail result={notObserved.data} />
          ) : (
            (() => {
              const state = notObservedUnavailable(notObserved.error);
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
          )}
        </div>
      ) : null}
    </section>
  );
}
