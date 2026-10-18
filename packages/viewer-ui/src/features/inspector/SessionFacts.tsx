import { ApiError } from "../../api/client";
import { formatDuration, formatMeasurement } from "../../lib/format";
import { toneColorVar } from "../../lib/status";
import {
  roleColorVar,
  type ClockCalibrationView,
  type ContradictionView,
  type DiagnosisView,
  type UnassignedFacts,
} from "./timeline";
import styles from "./SessionFacts.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

/** Backend-authored diagnoses. Each links to the operations named in its
 * evidence so the reader can jump to the fact it rests on. Diagnoses come only
 * from the explanation — the viewer never derives one. */
export function DiagnosesPanel({
  diagnoses,
  onSelectEvidence,
}: {
  diagnoses: DiagnosisView[];
  onSelectEvidence: (turnIndex: number, operationId: string) => void;
}) {
  if (diagnoses.length === 0) return null;
  return (
    <section className={styles.panel} aria-label="Diagnoses">
      <div className={styles.panelHead}>
        <h2>Diagnoses</h2>
        <span className={styles.count}>{diagnoses.length}</span>
      </div>
      <div className={styles.list}>
        {diagnoses.map((d) => (
          <article key={d.id} className={styles.diag}>
            <div className={styles.diagHead}>
              <span className={styles.code}>{d.code}</span>
              <span className={styles.conf}>{d.confidence}</span>
            </div>
            <p className={styles.summary}>{d.summary}</p>
            <div className={styles.evidence}>
              <span className={styles.evLabel}>evidence</span>
              {d.evidence.map((e) =>
                e.turnIndex != null ? (
                  <button
                    key={e.id}
                    type="button"
                    className={styles.evChip}
                    onClick={() => onSelectEvidence(e.turnIndex as number, e.id)}
                  >
                    {e.id}
                  </button>
                ) : (
                  <span key={e.id} className={`${styles.evChip} ${styles.evStatic}`}>
                    {e.id}
                  </span>
                ),
              )}
            </div>
            {d.limitations.length > 0 ? (
              <p className={styles.limit}>{d.limitations.join(" · ")}</p>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

/** Whether contradiction detection ran, and what it said. `pending` and
 * `unavailable` are distinct states on purpose: only `ready` may be read as
 * "these are the conflicts in this session". */
export type ContradictionsStatus = "pending" | "unavailable" | "ready";

/** Why contradiction detection has no answer. A backend refusal carries its own
 * stable code, which is the useful thing to show; anything else (an unreachable
 * or non-answering backend) is reported as exactly that rather than guessed at. */
export function contradictionsReason(error: unknown): string {
  return error instanceof ApiError ? error.message : "the backend did not answer";
}

/** Backend-detected contradictions: two observations in one incident that cannot
 * both be true. Detection is the backend's; the viewer only renders the verdict
 * and links each cited operation to the turn that owns it. */
export function ContradictionsPanel({
  status,
  reason,
  contradictions,
  onSelectEvidence,
}: {
  status: ContradictionsStatus;
  /** Why detection could not run. Required reading when `status` is `unavailable`. */
  reason: string | null;
  contradictions: ContradictionView[];
  onSelectEvidence: (turnIndex: number, operationId: string) => void;
}) {
  return (
    <section className={styles.panel} aria-label="Contradictions">
      <div className={styles.panelHead}>
        <h2>Contradictions</h2>
        {status === "ready" ? (
          <span className={styles.count}>{contradictions.length}</span>
        ) : (
          <span className={styles.note}>
            {status === "pending" ? "checking…" : "not available"}
          </span>
        )}
      </div>
      {status === "unavailable" ? (
        // An absent answer is stated as absent. It is never rendered as zero
        // conflicts, which would read as a clean bill of health.
        <p className={styles.limit}>
          Contradiction detection did not run for this incident
          {reason == null ? "" : ` · ${reason}`}. Whether its observations conflict is
          unknown, not resolved.
        </p>
      ) : status === "pending" ? (
        <p className={styles.limit}>Checking this incident&rsquo;s evidence graph…</p>
      ) : contradictions.length === 0 ? (
        <p className={styles.limit}>
          Detection ran over this incident&rsquo;s evidence graph and found no pair of
          observations that contradict each other.
        </p>
      ) : (
        <div className={styles.list}>
          {contradictions.map((c) => (
            <article key={c.reactKey} className={styles.contra}>
              <div className={styles.diagHead}>
                <span className={styles.code}>{humanize(c.kind)}</span>
                {c.boundary != null ? (
                  <span className={styles.boundary}>{c.boundary}</span>
                ) : null}
                {c.turnId != null ? (
                  <span className={styles.note}>{c.turnId}</span>
                ) : null}
              </div>
              <p className={styles.summary}>{humanize(c.summary)}</p>
              <div className={styles.evidence}>
                <span className={styles.evLabel}>evidence</span>
                {c.evidence.map((e) =>
                  e.turnIndex != null ? (
                    <button
                      key={e.id}
                      type="button"
                      className={styles.evChip}
                      onClick={() => onSelectEvidence(e.turnIndex as number, e.id)}
                    >
                      {e.id}
                    </button>
                  ) : (
                    <span key={e.id} className={`${styles.evChip} ${styles.evStatic}`}>
                      {e.id}
                    </span>
                  ),
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

/** Cross-clock comparability: the declared clock domains and calibrations, and
 * every derived latency whose value — or absence — those calibrations decide.
 * A latency missing because two facts sit in unrelated clock domains says so
 * here rather than disappearing from the timeline. */
export function ClockCalibrationPanel({
  calibration,
}: {
  calibration: ClockCalibrationView;
}) {
  const { domains, relations, crossClock } = calibration;
  // Nothing to report when the session lives on one clock and no derived latency
  // depends on relating two.
  if (domains.length < 2 && relations.length === 0 && crossClock.length === 0)
    return null;
  return (
    <section className={styles.panel} aria-label="Clock comparability">
      <div className={styles.panelHead}>
        <h2>Clock comparability</h2>
        <span className={styles.note}>
          {domains.length} clock {domains.length === 1 ? "domain" : "domains"} ·{" "}
          {relations.length} declared{" "}
          {relations.length === 1 ? "calibration" : "calibrations"}
        </span>
      </div>

      <div className={styles.measBlock}>
        {domains.map((domain) => (
          <div key={domain.id} className={styles.measRow}>
            <span className={styles.measName}>{domain.id}</span>
            <span className={styles.measVal}>{domain.kind}</span>
            <span className={styles.measConf}>{domain.observer}</span>
          </div>
        ))}
      </div>

      {relations.length === 0 ? (
        <p className={styles.limit}>
          No clock calibration is declared, so facts recorded in different clock domains
          cannot be related and no latency is derived across them.
        </p>
      ) : (
        <div className={styles.measBlock}>
          {relations.map((relation) => (
            <div key={relation.relationId} className={styles.measRow}>
              <span className={styles.measName}>
                {relation.fromDomain} → {relation.toDomain}
              </span>
              <span className={styles.measVal}>
                {relation.uncertaintyMs == null
                  ? // The relation declares no error bound: unknown, not zero.
                    "uncertainty not declared"
                  : `±${formatDuration(relation.uncertaintyMs)}`}
              </span>
              <span className={styles.measConf}>{relation.method}</span>
            </div>
          ))}
        </div>
      )}

      {crossClock.length > 0 ? (
        <div className={styles.measBlock}>
          {crossClock.map((row) => (
            <div key={row.reactKey} className={styles.measRow}>
              <span className={styles.measName}>
                T{String(row.turnIndex).padStart(2, "0")} · {row.metric}
              </span>
              <span
                className={`${styles.measVal} ${row.state === "unavailable" ? styles.dim : ""}`}
              >
                {row.state === "estimated" ? "estimated" : humanize(row.availability)}
              </span>
              <span className={styles.reason}>{row.note}</span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** Operations/events without a turn and measurements without any authored owner.
 * Rendering them keeps genuinely session-level evidence visible. */
export function UnassignedPanel({ facts }: { facts: UnassignedFacts }) {
  if (
    facts.operations.length === 0 &&
    facts.events.length === 0 &&
    facts.measurements.length === 0
  )
    return null;
  return (
    <section className={styles.panel} aria-label="Session-level facts">
      <div className={styles.panelHead}>
        <h2>Session-level facts</h2>
        <span className={styles.note}>no authored turn or stage owner</span>
      </div>

      {facts.operations.length > 0 ? (
        <div className={styles.list}>
          {facts.operations.map((op) => (
            <article key={op.operationId} className={styles.unop}>
              <div className={styles.unopHead}>
                <span
                  className={styles.dot}
                  style={{ background: roleColorVar(op.role) }}
                />
                <span className={styles.unName}>{op.name}</span>
                {op.statusView.abnormal ? (
                  <span
                    className={styles.statusBadge}
                    style={{
                      color: toneColorVar(op.statusView.tone),
                      borderColor: toneColorVar(op.statusView.tone),
                    }}
                  >
                    {op.statusView.label}
                  </span>
                ) : null}
                <span className={styles.unDur}>
                  {op.durationMs == null ? "point" : formatDuration(op.durationMs)}
                </span>
              </div>
              {op.measurements.map((m) => (
                <div key={m.reactKey} className={styles.measRow}>
                  <span className={styles.measName}>{m.name}</span>
                  <span className={styles.measVal}>
                    {formatMeasurement(m.value, m.unit)}
                  </span>
                  <span className={styles.measConf}>{m.confidence}</span>
                </div>
              ))}
            </article>
          ))}
        </div>
      ) : null}

      {facts.events.length > 0 ? (
        <div className={styles.list}>
          {facts.events.map((event) => (
            <article key={event.eventId} className={styles.unop}>
              <div className={styles.unopHead}>
                <span className={styles.unName}>{event.name}</span>
                <span className={styles.measConf}>{event.confidence}</span>
                <span className={styles.unDur}>{event.coordinate}</span>
              </div>
            </article>
          ))}
        </div>
      ) : null}

      {facts.measurements.length > 0 ? (
        <div className={styles.measBlock}>
          {facts.measurements.map((m) => (
            <div key={m.reactKey} className={styles.measRow}>
              <span className={styles.measName}>{m.name}</span>
              <span className={styles.measVal}>{formatMeasurement(m.value, m.unit)}</span>
              <span className={styles.measConf}>{m.confidence}</span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
