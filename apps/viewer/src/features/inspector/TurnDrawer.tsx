import { CallGraph } from "./CallGraph";
import styles from "./drawer.module.css";
import type { CoverageRow, StageName, TurnDetail } from "./timeline";

const short = (name: string) => name.replace(/^earshot\./, "");
const humanize = (s: string) => s.replace(/_/g, " ");

function glyphColor(name: string): string {
  if (name.includes("interruption")) return "var(--tts)";
  if (name.includes("transcript")) return "var(--stt)";
  return "var(--tx-low)";
}

export function TurnDrawer({
  detail,
  coverage,
  onClose,
  onPickStage,
}: {
  detail: TurnDetail;
  coverage: CoverageRow[];
  onClose: () => void;
  onPickStage: (stage: StageName) => void;
}) {
  const ft = detail.firstTokenMs;
  const slow = (ft ?? 0) > 500;
  const budget =
    ft == null ? "not observed" : slow ? "well over budget" : "within budget";

  return (
    <aside className={styles.drawer} aria-label={`Turn ${detail.index} detail`}>
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
          <span className={styles.dot} style={{ background: "var(--acc)" }} />
          <span className={styles.title}>
            turn {String(detail.index).padStart(2, "0")}
          </span>
          {detail.interrupted ? (
            <span className={`${styles.chip} ${styles.barge}`}>barge-in</span>
          ) : null}
          {slow ? <span className={`${styles.chip} ${styles.slow}`}>slow</span> : null}
        </div>
        <div className={styles.hero}>
          <span className={`${styles.big} ${slow ? styles.critical : ""}`}>
            {ft == null ? "—" : ft}
            {ft == null ? null : <small> ms</small>}
          </span>
          <span className={styles.heroLbl}>first token · {budget}</span>
        </div>
      </div>

      <div className={styles.body}>
        <section className={styles.sec}>
          <span className={styles.secLabel}>Call graph</span>
          <CallGraph detail={detail} onPick={onPickStage} />
        </section>

        <section className={styles.sec}>
          <span className={styles.secLabel}>Derived metrics</span>
          {detail.metrics.map((m) => (
            <div key={m.key} className={styles.metricLine}>
              <span className={styles.mln}>{m.key}</span>
              <span className={`${styles.mlv} ${m.value == null ? styles.na : ""}`}>
                {m.value == null ? humanize(m.availability) : `${m.value}ms`}
              </span>
              <span className={styles.mlb}>{m.basis}</span>
            </div>
          ))}
        </section>

        <section className={styles.sec}>
          <span className={styles.secLabel}>Events</span>
          {detail.events.map((e, i) => (
            <div key={`${e.name}-${i}`} className={styles.evrow}>
              <span className={styles.glyph} style={{ background: glyphColor(e.name) }} />
              <span className={styles.en}>{short(e.name)}</span>
              <span className={styles.et}>
                {e.atMs == null ? "offset unavailable" : `+${Math.round(e.atMs)}ms`} ·{" "}
                {e.confidence}
              </span>
            </div>
          ))}
        </section>

        <section className={styles.sec}>
          <span className={styles.secLabel}>Coverage · not observed</span>
          {coverage.map((c) => (
            <div key={c.signal} className={styles.metricLine}>
              <span className={styles.mln}>{c.signal}</span>
              <span className={`${styles.mlv} ${styles.na}`}>
                {humanize(c.availability)}
              </span>
              <span className={styles.mlb}>{c.reason ?? ""}</span>
            </div>
          ))}
          <div className={styles.note}>
            Earshot never guesses. A server-side pipeline can't see when the caller{" "}
            <b>heard</b> the reply, so response &amp; render stay <b>not observed</b>{" "}
            rather than fabricated.
          </div>
        </section>
      </div>
    </aside>
  );
}
