import { formatMs } from "../../lib/format";
import styles from "./drawer.module.css";
import type { StageDetail } from "./timeline";

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

  return (
    <aside className={styles.drawer} aria-label={`Turn ${index} ${stage.name} detail`}>
      <div className={styles.head}>
        <button
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
            {Math.round(stage.leadMs)}
            <small> ms</small>
          </span>
          <span className={styles.heroLbl}>{LEAD_LABEL[stage.name]}</span>
        </div>
      </div>

      <div className={styles.body}>
        <section className={styles.sec}>
          <span className={styles.secLabel}>Stage</span>
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
            <span className={styles.kk}>window</span>
            <span className={styles.vv}>
              +{Math.round(stage.startMs)} → +{Math.round(stage.endMs)} ms
            </span>
          </div>
        </section>

        <section className={styles.sec}>
          <span className={styles.secLabel}>Provider measurement</span>
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
            <span className={styles.secLabel}>Evidence · provenance</span>
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
              The <b>latency</b> is provider-measured; the stage <b>interval</b> is{" "}
              <span className={`${styles.conf} ${styles.inferred}`}>inferred</span> — a
              scalar can't prove a boundary Earshot didn't observe.
            </div>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
