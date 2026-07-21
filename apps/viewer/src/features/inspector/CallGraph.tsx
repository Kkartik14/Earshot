import { useState } from "react";
import { formatMs } from "../../lib/format";
import styles from "./CallGraph.module.css";
import type { StageDetail, TurnDetail } from "./timeline";

const STAGE_LABEL: Record<string, string> = {
  stt: "speech-to-text",
  llm: "language model",
  tts: "text-to-speech",
};

function confClass(confidence: string): string {
  if (confidence === "measured") return styles.measured;
  if (confidence === "inferred") return styles.inferred;
  return styles.weak;
}

function StageNode({ stage, badge }: { stage: StageDetail; badge?: string }) {
  const [open, setOpen] = useState(false);
  const expandable = stage.evidence != null || stage.measurements.length > 0;

  return (
    <li className={`${styles.node} ${styles[stage.name]}`}>
      <span className={styles.dot} />
      <button
        type="button"
        className={styles.card}
        onClick={() => expandable && setOpen((v) => !v)}
        aria-expanded={expandable ? open : undefined}
        data-static={expandable ? undefined : ""}
      >
        <div className={styles.head}>
          <span className={styles.stage}>{stage.name}</span>
          <span className={styles.kind}>{STAGE_LABEL[stage.name]}</span>
          {badge ? <span className={styles.badge}>{badge}</span> : null}
          <span className={styles.lead}>{formatMs(stage.leadMs)}</span>
          {expandable ? (
            <span className={`${styles.chev} ${open ? styles.chevOpen : ""}`}>›</span>
          ) : null}
        </div>
        <div className={styles.model}>
          {stage.provider ?? "unknown"} · {stage.model ?? "unknown"}
        </div>

        {open ? (
          <div className={styles.detail}>
            {stage.evidence ? (
              <dl className={styles.evidence}>
                <div>
                  <dt>source</dt>
                  <dd>{stage.evidence.source}</dd>
                </div>
                <div>
                  <dt>observer</dt>
                  <dd>{stage.evidence.observer}</dd>
                </div>
                <div>
                  <dt>method</dt>
                  <dd>{stage.evidence.method}</dd>
                </div>
                <div>
                  <dt>confidence</dt>
                  <dd className={confClass(stage.evidence.confidence)}>
                    {stage.evidence.confidence}
                  </dd>
                </div>
              </dl>
            ) : null}
            {stage.measurements.length > 0 ? (
              <ul className={styles.meas}>
                {stage.measurements.map((m) => (
                  <li key={m.name}>
                    <code>{m.name}</code>
                    <span>{formatMs(m.value)}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </button>
    </li>
  );
}

function Endpoint({ label, sub }: { label: string; sub: string }) {
  return (
    <li className={`${styles.node} ${styles.endpoint}`}>
      <span className={styles.dot} />
      <div className={styles.card} data-static="">
        <div className={styles.head}>
          <span className={styles.kind}>{label}</span>
          <span className={styles.lead}>{sub}</span>
        </div>
      </div>
    </li>
  );
}

export function CallGraph({ detail }: { detail: TurnDetail }) {
  const response = detail.metrics.find((m) => m.key === "response");
  const heard = detail.events.some((e) => e.name === "earshot.transcript.final");

  return (
    <ol className={styles.graph}>
      <Endpoint label="user speech" sub={heard ? "transcribed" : "captured"} />
      {detail.stages.map((stage) => (
        <StageNode
          key={stage.name}
          stage={stage}
          badge={
            stage.name === "llm" && detail.firstTokenMs != null
              ? `first token ${formatMs(detail.firstTokenMs)}`
              : undefined
          }
        />
      ))}
      <Endpoint label="agent audio" sub={formatMs(response?.value ?? null)} />
    </ol>
  );
}
