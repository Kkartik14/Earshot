import { formatDuration, formatMs } from "../../lib/format";
import { statusTone } from "../../lib/status";
import styles from "./SessionHeader.module.css";
import type { SessionSummary } from "./timeline";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue}>{value}</span>
    </div>
  );
}

export function SessionHeader({ summary }: { summary: SessionSummary }) {
  return (
    <header className={styles.head}>
      <div className={styles.title}>
        <h1 className={styles.id}>{summary.sessionId}</h1>
        <span className={`${styles.pill} ${styles[statusTone(summary.status)]}`}>
          <span className={styles.led} />
          {summary.status}
        </span>
      </div>

      <div className={styles.stack}>
        {summary.stack.map((entry) => (
          <span key={entry} className={styles.chip}>
            {entry}
          </span>
        ))}
      </div>

      <div className={styles.stats}>
        <Stat label="Turns" value={String(summary.turns)} />
        <Stat label="Duration" value={formatDuration(summary.durationMs)} />
        <Stat label="p95 first-token" value={formatMs(summary.p95FirstTokenMs)} />
        <Stat label="Interruptions" value={String(summary.interruptions)} />
      </div>
    </header>
  );
}
