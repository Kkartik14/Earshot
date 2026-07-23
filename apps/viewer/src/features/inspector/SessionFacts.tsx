import { formatDuration, formatMeasurement } from "../../lib/format";
import { toneColorVar } from "../../lib/status";
import { roleColorVar, type DiagnosisView, type UnassignedFacts } from "./timeline";
import styles from "./SessionFacts.module.css";

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

/** Operations and measurements the analyzer could not scope to a turn. Rendering
 * them keeps an incident whose evidence is session-level (webrtc jitter/rtt,
 * a device-unavailable operation) visible instead of an empty inspector. */
export function UnassignedPanel({ facts }: { facts: UnassignedFacts }) {
  if (facts.operations.length === 0 && facts.measurements.length === 0) return null;
  return (
    <section className={styles.panel} aria-label="Session-level facts">
      <div className={styles.panelHead}>
        <h2>Session-level facts</h2>
        <span className={styles.note}>not scoped to a turn</span>
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
                <div key={m.name} className={styles.measRow}>
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

      {facts.measurements.length > 0 ? (
        <div className={styles.measBlock}>
          {facts.measurements.map((m) => (
            <div key={m.name} className={styles.measRow}>
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
