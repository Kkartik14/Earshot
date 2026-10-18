import { formatMeasurement } from "../../lib/format";
import { toneColorVar } from "../../lib/status";
import styles from "./drawer.module.css";
import {
  roleColorVar,
  roleLabel,
  type OperationRole,
  type StageDetail,
} from "./timeline";
import { useInitialFocus } from "./useInitialFocus";

// Cascade stages get a friendlier hero label; other roles fall back to a
// generic observed-duration readout.
const LEAD_LABEL: Partial<Record<OperationRole, string>> = {
  stt: "time to first response",
  llm: "time to first token",
  tts: "time to first byte",
};

function confClass(confidence: string): string {
  return styles[confidence] ?? styles.unavailable;
}

const relationshipLabel = (relationship: string): string =>
  relationship.replace(/_/g, " ");

export function StageDrawer({
  index,
  stage,
  onClose,
}: {
  index: number;
  stage: StageDetail;
  onClose: () => void;
}) {
  const color = roleColorVar(stage.role);
  const ev = stage.evidence;
  const status = stage.statusView;
  const badgeColor = toneColorVar(status.tone);

  const leadLabel = LEAD_LABEL[stage.role];
  const observedDuration =
    stage.timing === "interval" && stage.startMs != null && stage.endMs != null
      ? stage.endMs - stage.startMs
      : null;
  const heroMs = stage.leadMs ?? observedDuration;
  const heroLabel =
    stage.leadMs != null && leadLabel != null
      ? leadLabel
      : observedDuration != null
        ? "observed duration"
        : roleLabel(stage.role);

  // Focus the close control on open / operation change: keyboard and SR users
  // land inside the labelled dialog on a visibly focusable element.
  const closeButton = useInitialFocus<HTMLButtonElement>(`${index}:${stage.operationId}`);

  return (
    <aside
      role="dialog"
      aria-label={`Turn ${index} ${stage.name} detail`}
      className={styles.drawer}
    >
      <div className={styles.head}>
        <button
          ref={closeButton}
          type="button"
          className={styles.close}
          onClick={onClose}
          aria-label="Close detail"
        >
          ×
        </button>
        <div className={styles.kind}>
          <span className={styles.dot} style={{ background: color }} />
          <span className={styles.title} style={{ color }}>
            {stage.name}
          </span>
          <span className={styles.kindTag}>{roleLabel(stage.role)}</span>
          {status.abnormal ? (
            <span
              className={styles.statusBadge}
              style={{ color: badgeColor, borderColor: badgeColor }}
            >
              {status.label}
            </span>
          ) : null}
        </div>
        <div className={styles.hero}>
          <span className={styles.big}>
            {heroMs == null ? "—" : formatMeasurement(heroMs, "ms")}
          </span>
          <span className={styles.heroLbl}>{heroLabel}</span>
        </div>
      </div>

      <div className={styles.body}>
        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Operation</h2>
          <div className={styles.kv}>
            <span className={styles.kk}>provider</span>
            <span className={styles.vv}>{stage.provider ?? "unknown"}</span>
          </div>
          <div className={styles.kv}>
            <span className={styles.kk}>model / voice</span>
            <span className={styles.vv}>{stage.model ?? "unknown"}</span>
          </div>
          <div className={styles.kv}>
            <span className={styles.kk}>status</span>
            <span
              className={styles.vv}
              style={status.abnormal ? { color: badgeColor } : undefined}
            >
              {stage.status}
            </span>
          </div>
          {status.error ? (
            <div className={styles.kv}>
              <span className={styles.kk}>error</span>
              <span className={styles.vv} style={{ color: badgeColor }}>
                {status.error.code} · {status.error.category}
              </span>
            </div>
          ) : null}
          <div className={styles.kv}>
            <span className={styles.kk}>timing</span>
            <span className={styles.vv}>
              {stage.timing === "interval" && stage.startMs != null && stage.endMs != null
                ? `observed interval +${Math.round(stage.startMs)} → +${Math.round(stage.endMs)} ms`
                : stage.timing === "point" && stage.startMs != null
                  ? `observed point +${Math.round(stage.startMs)} ms; interval not observed`
                  : "operation timing unavailable"}
              {stage.startUncertaintyMs != null
                ? ` (±${Math.round(stage.startUncertaintyMs)} ms)`
                : ""}
            </span>
          </div>
          {stage.interruptedByEvent ? (
            <div className={styles.kv}>
              <span className={styles.kk}>interrupted by</span>
              <span className={styles.vv}>{stage.interruptedByEvent}</span>
            </div>
          ) : null}
        </section>

        {stage.links.length > 0 ? (
          <section className={styles.sec}>
            <h2 className={styles.secLabel}>Causal links</h2>
            {stage.links.map((link, i) => (
              <div key={`${link.relationship}-${i}`} className={styles.kv}>
                <span className={styles.kk}>{relationshipLabel(link.relationship)}</span>
                <span className={styles.vv}>
                  {link.resolved && link.targetOperationId != null
                    ? link.targetOperationId
                    : `${link.targetScope} target`}
                </span>
              </div>
            ))}
          </section>
        ) : null}

        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Provider measurement</h2>
          {stage.measurements.length > 0 ? (
            stage.measurements.map((m) => (
              <div key={m.reactKey} className={styles.mrow}>
                <span className={styles.mn}>{m.name}</span>
                <span className={styles.mv} style={{ color }}>
                  {formatMeasurement(m.value, m.unit)}
                </span>
                <span className={`${styles.conf} ${confClass(m.confidence)}`}>
                  {m.confidence}
                </span>
              </div>
            ))
          ) : (
            <div className={styles.note}>No provider measurement on this operation.</div>
          )}
        </section>

        {ev ? (
          <section className={styles.sec}>
            <h2 className={styles.secLabel}>Evidence · provenance</h2>
            <div className={styles.provFlow}>
              <b>{ev.source}</b>
              <span className={styles.ar}>→</span>
              <b>{ev.observer}</b>
              <span className={styles.ar}>→</span>
              {ev.method}
            </div>
            {ev.sourceField ? (
              <div className={styles.kv} style={{ marginTop: 8 }}>
                <span className={styles.kk}>source field</span>
                <span className={styles.vv}>{ev.sourceField}</span>
              </div>
            ) : null}
            <div className={styles.note}>
              A provider scalar describes the named <b>measurement</b>. It does not create
              an operation interval; intervals appear only when both boundaries were
              observed.
            </div>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
