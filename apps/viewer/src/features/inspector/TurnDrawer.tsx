import { formatMeasurement } from "../../lib/format";
import { CallGraph } from "./CallGraph";
import styles from "./drawer.module.css";
import {
  clockComparability,
  type CoverageRow,
  type InterruptionChainView,
  type MetricRow,
  type TurnDetail,
} from "./timeline";
import { useInitialFocus } from "./useInitialFocus";

const short = (name: string) => name.replace(/^earshot\./, "");
const humanize = (s: string) => s.replace(/_/g, " ");

/** The right-hand readout for a derived metric: the measured value, or the exact
 * reason there is none. Never a blank, a dash, or a zero standing in for unknown. */
function metricReadout(metric: MetricRow): { value: string; reason: string } {
  const clock = clockComparability(metric);
  if (metric.value != null) {
    return {
      value: formatMeasurement(metric.value, "ms"),
      reason: clock?.state === "estimated" ? `estimated · ${clock.note}` : metric.basis,
    };
  }
  const reason =
    clock?.note ??
    (metric.limitation != null ? humanize(metric.limitation) : metric.basis);
  return { value: humanize(metric.availability), reason };
}

/** One interruption episode: the analyzer's ordered stages and the barge-in
 * latency. An unobserved stage shows its coverage reason and a latency that could
 * not be derived shows what stopped it — neither is drawn as a success. */
function InterruptionChain({
  chain,
  ordinal,
  total,
}: {
  chain: InterruptionChainView;
  ordinal: number;
  total: number;
}) {
  const effectiveness = metricReadout(chain.effectiveness);
  const label = total > 1 ? `Interruption ${ordinal + 1} of ${total}` : "Interruption";
  return (
    <article aria-label={`${label} · ${chain.classification}`}>
      <div className={styles.chainHead}>
        <span className={styles.title}>{label}</span>
        <span className={`${styles.chip} ${styles.barge}`}>{chain.classification}</span>
      </div>
      <div className={styles.metricLine}>
        <span className={styles.mln}>barge-in effectiveness</span>
        <span
          className={`${styles.mlv} ${chain.effectiveness.value == null ? styles.na : ""}`}
        >
          {effectiveness.value}
        </span>
        <span className={styles.mlb}>{effectiveness.reason}</span>
      </div>
      <ol className={styles.chain}>
        {chain.stages.map((stage) => (
          <li
            key={stage.stage}
            className={`${styles.chainStage} ${stage.observed ? "" : styles.chainMissing}`}
          >
            <span className={styles.chainName}>
              {humanize(stage.stage)}
              {stage.outcome != null ? (
                <span className={styles.evAttach}> → {stage.outcome}</span>
              ) : null}
            </span>
            <span className={styles.chainWhen}>
              {!stage.observed
                ? `not observed · ${humanize(stage.coverageReason ?? "reason not stated")}`
                : stage.atMs != null
                  ? `+${Math.round(stage.atMs)}ms`
                  : (stage.coordinate ?? "coordinate not comparable")}
            </span>
          </li>
        ))}
      </ol>
    </article>
  );
}

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
  onPickStage: (operationId: string) => void;
}) {
  const ft = detail.firstTokenMs;

  // Move focus to the close control when the panel opens or its turn changes,
  // so keyboard and screen-reader users land inside the labelled dialog on a
  // visibly focusable element.
  const closeButton = useInitialFocus<HTMLButtonElement>(detail.index);

  return (
    <aside
      role="dialog"
      aria-label={`Turn ${detail.index} detail`}
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
          <span className={styles.dot} style={{ background: "var(--acc)" }} />
          <span className={styles.title}>
            turn {String(detail.index).padStart(2, "0")}
          </span>
          {detail.interrupted ? (
            <span className={`${styles.chip} ${styles.barge}`}>barge-in</span>
          ) : null}
        </div>
        <div className={styles.hero}>
          <span className={styles.big}>
            {ft == null ? "—" : ft}
            {ft == null ? null : <small> ms</small>}
          </span>
          <span className={styles.heroLbl}>first token</span>
        </div>
      </div>

      <div className={styles.body}>
        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Call graph</h2>
          <CallGraph detail={detail} onPick={onPickStage} />
        </section>

        {detail.interruptionChains.length > 0 ? (
          <section className={styles.sec}>
            <h2 className={styles.secLabel}>Interruption chain</h2>
            {detail.interruptionChains.map((chain, index) => (
              <InterruptionChain
                key={chain.reactKey}
                chain={chain}
                ordinal={index}
                total={detail.interruptionChains.length}
              />
            ))}
          </section>
        ) : null}

        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Derived metrics</h2>
          {detail.metrics.map((m) => {
            const readout = metricReadout(m);
            return (
              <div key={m.key} className={styles.metricLine}>
                <span className={styles.mln}>{m.key}</span>
                <span className={`${styles.mlv} ${m.value == null ? styles.na : ""}`}>
                  {readout.value}
                </span>
                <span className={styles.mlb}>{readout.reason}</span>
              </div>
            );
          })}
        </section>

        {detail.measurements.length > 0 ? (
          <section className={styles.sec}>
            <h2 className={styles.secLabel}>Measurement facts</h2>
            {detail.measurements.map((measurement) => (
              <div key={measurement.reactKey} className={styles.metricLine}>
                <span className={styles.mln}>{measurement.name}</span>
                <span className={styles.mlv}>
                  {formatMeasurement(measurement.value, measurement.unit)}
                </span>
                <span className={styles.mlb}>
                  {[measurement.sourceField, ...measurement.evidenceIds]
                    .filter((value) => value != null && value !== "")
                    .join(" · ")}
                </span>
              </div>
            ))}
          </section>
        ) : null}

        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Events</h2>
          {detail.events.map((e, i) => (
            <div key={`${e.name}-${i}`} className={styles.evrow}>
              <span className={styles.glyph} style={{ background: glyphColor(e.name) }} />
              <span className={styles.en}>
                {short(e.name)}
                {e.attachedOperationId != null ? (
                  <span className={styles.evAttach}> → {e.attachedOperationId}</span>
                ) : null}
              </span>
              <span className={styles.et}>
                {e.atMs == null ? "offset unavailable" : `+${Math.round(e.atMs)}ms`} ·{" "}
                {e.confidence}
              </span>
            </div>
          ))}
        </section>

        <section className={styles.sec}>
          <h2 className={styles.secLabel}>Coverage · not observed</h2>
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
