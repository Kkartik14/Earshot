import { formatMs } from "../../lib/format";
import { CallGraph } from "./CallGraph";
import styles from "./TurnDrawer.module.css";
import type { CoverageRow, TurnDetail } from "./timeline";

const short = (name: string) => name.replace(/^earshot\./, "");

function confClass(confidence: string): string {
  if (confidence === "measured") return styles.good;
  if (confidence === "inferred") return styles.warn;
  return styles.muted;
}

export function TurnDrawer({
  detail,
  coverage,
  onClose,
}: {
  detail: TurnDetail;
  coverage: CoverageRow[];
  onClose: () => void;
}) {
  return (
    <aside className={styles.drawer} aria-label={`Turn ${detail.index} detail`}>
      <header className={styles.head}>
        <div className={styles.title}>
          <span className={styles.tnum}>T{String(detail.index).padStart(2, "0")}</span>
          {detail.interrupted ? <span className={styles.barge}>barge-in</span> : null}
        </div>
        <button
          type="button"
          className={styles.close}
          onClick={onClose}
          aria-label="Close detail"
        >
          ×
        </button>
      </header>

      <div className={styles.body}>
        <section className={styles.section}>
          <h3 className={styles.heading}>Call graph</h3>
          <CallGraph detail={detail} />
        </section>

        <section className={styles.section}>
          <h3 className={styles.heading}>Latency metrics</h3>
          <ul className={styles.metrics}>
            {detail.metrics.map((m) => (
              <li key={m.key} className={styles.metricRow}>
                <span className={styles.metricKey}>{m.key.replace(/_/g, " ")}</span>
                <span className={styles.metricVal}>{formatMs(m.value)}</span>
                <span className={`${styles.tag} ${confClass(m.confidence)}`}>
                  {m.value == null ? m.availability : m.confidence}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.section}>
          <h3 className={styles.heading}>Events</h3>
          <ul className={styles.events}>
            {detail.events.map((e, i) => (
              <li key={`${e.name}-${i}`} className={styles.eventRow}>
                <span className={styles.at}>{formatMs(e.atMs)}</span>
                <span className={styles.eventName}>{short(e.name)}</span>
                {e.participant ? (
                  <span className={styles.who}>{e.participant}</span>
                ) : null}
                <span
                  className={`${styles.led} ${confClass(e.confidence)}`}
                  title={e.confidence}
                />
              </li>
            ))}
          </ul>
        </section>

        {coverage.length > 0 ? (
          <section className={styles.section}>
            <h3 className={styles.heading}>Coverage gaps</h3>
            <ul className={styles.gaps}>
              {coverage.map((c) => (
                <li key={c.signal} className={styles.gapRow}>
                  <code className={styles.gapSignal}>{c.signal}</code>
                  <span className={styles.gapReason}>
                    {(c.reason ?? c.availability).replace(/_/g, " ")}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
