import { formatMs } from "../../lib/format";
import styles from "./drawer.module.css";
import type { StageDetail } from "./timeline";
import { useInitialFocus } from "./useInitialFocus";

const STAGE_LABEL: Record<string, string> = {
  stt: "listen",
  llm: "think",
  tts: "speak",
};
const LEAD_LABEL: Record<string, string> = {
  stt: "finalization",
  llm: "time to first token",
  tts: "time to first byte",
};

function confClass(confidence: string): string {
  return styles[confidence] ?? styles.unavailable;
}

export function StageDrawer({
  index,
  stage,
  onClose,
}: {
  index: number;
  stage: StageDetail;
  onClose: () => void;
}) {
  const color = `var(--${stage.name})`;
  const ev = stage.evidence;

  // Focus the close control on open / stage change: keyboard and SR users land
  // inside the labelled dialog on a visibly focusable element.
  const closeButton = useInitialFocus<HTMLButtonElement>(`${index}:${stage.name}`);

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
          <span className={styles.kindTag}>{STAGE_LABEL[stage.name]}</span>
        </div>
        <div className={styles.hero}>
          <span className={styles.big}>
            {stage.leadMs == null ? "—" : Math.round(stage.leadMs)}
            {stage.leadMs == null ? null : <small> ms</small>}
          </span>
          <span className={styles.heroLbl}>{LEAD_LABEL[stage.name]}</span>
        </div>
      </div>

      <div className={styles.body}>
        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Stage</h2>
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
            <span className={styles.vv}>{stage.status}</span>
          </div>
          <div className={styles.kv}>
            <span className={styles.kk}>timing</span>
            <span className={styles.vv}>
              {stage.timing === "interval" && stage.startMs != null && stage.endMs != null
                ? `observed interval +${Math.round(stage.startMs)} → +${Math.round(stage.endMs)} ms`
                : stage.timing === "point" && stage.startMs != null
                  ? `observed point +${Math.round(stage.startMs)} ms; interval not observed`
                  : "stage timing unavailable"}
            </span>
          </div>
        </section>

        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Provider measurement</h2>
          {stage.measurements.length > 0 ? (
            stage.measurements.map((m) => (
              <div key={m.name} className={styles.mrow}>
                <span className={styles.mn}>{m.name}</span>
                <span className={styles.mv} style={{ color }}>
                  {formatMs(m.value)}
                </span>
                <span className={`${styles.conf} ${confClass(m.confidence)}`}>
                  {m.confidence}
                </span>
              </div>
            ))
          ) : (
            <div className={styles.note}>No provider measurement on this stage.</div>
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
              A provider scalar describes the named <b>latency</b>. It does not create a
              stage interval; intervals appear only when both boundaries were observed.
            </div>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
