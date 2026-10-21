import { useId } from "react";
import type { CallTurn, TurnSpan } from "./api";
import { formatClock, formatSeconds, gapReadout, humanize } from "./format";
import type { MediaClock } from "./mediaClock";
import styles from "./CallView.module.css";

/** The spans a turn offers details for: model and tool calls, and any error. */
export function detailSpans(turn: CallTurn): TurnSpan[] {
  return turn.spans.filter(
    (span) => span.role === "llm" || span.role === "tool" || span.status === "error",
  );
}

function spanButtonLabel(span: TurnSpan): string {
  const kind = span.role === "llm" ? "LLM" : span.role === "tool" ? "Tool" : span.role;
  return span.status === "error"
    ? `${kind} ${span.name} (error)`
    : `${kind} ${span.name}`;
}

const BASIS_NOTE: Record<NonNullable<CallTurn["audible_gap"]["basis"]>, string> = {
  anchors_checked: "recorder timeline checked",
  continuous_timeline_assumed: "continuous recorder timeline assumed",
};

/** Per turn: the framework's reported latency beside the audible gap measured at
 * the agent from the recording. Neither is ever shown as 0 when it is unknown. */
export function TurnsTable({
  turns,
  clock,
  openSpanId,
  onOpenSpan,
}: {
  turns: CallTurn[];
  clock: MediaClock | null;
  openSpanId: string | null;
  onOpenSpan: (span: TurnSpan, opener: HTMLElement) => void;
}) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.panelTitle}>
        Turns
      </h2>
      {turns.length === 0 ? (
        <p className={styles.note}>The framework reported no turns for this call.</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <caption className={styles.caption}>
              Reported latency is what the framework measured; the audible gap is measured
              at the agent from the recording.
            </caption>
            <thead>
              <tr>
                <th scope="col">Turn</th>
                <th scope="col">Starts</th>
                <th scope="col" className={styles.num}>
                  Reported latency
                </th>
                <th scope="col">Audible gap at the agent</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {turns.map((turn) => {
                const gap = gapReadout(turn.audible_gap);
                const number = turn.index + 1;
                const basis = turn.audible_gap.basis;
                return (
                  <tr key={turn.index}>
                    <th scope="row">{number}</th>
                    <td>
                      {turn.start_ms == null ? (
                        "—"
                      ) : clock == null ? (
                        formatClock(turn.start_ms)
                      ) : (
                        <button
                          type="button"
                          className={styles.seek}
                          aria-label={`Play turn ${number} from ${formatClock(turn.start_ms)}`}
                          onClick={() => clock.seek(turn.start_ms as number)}
                        >
                          {formatClock(turn.start_ms)}
                        </button>
                      )}
                    </td>
                    <td className={styles.num}>
                      {turn.reported_latency_ms == null ? (
                        <span className={styles.dim}>not reported</span>
                      ) : (
                        formatSeconds(turn.reported_latency_ms)
                      )}
                    </td>
                    <td>
                      <span
                        className={styles.gapValue}
                        data-status={turn.audible_gap.status}
                        title={gap.reason ?? undefined}
                      >
                        {gap.value}
                      </span>
                      {gap.reason ? (
                        <span className={styles.reason}>{gap.reason}</span>
                      ) : null}
                      {basis != null && turn.audible_gap.status === "measured" ? (
                        <span className={styles.reason}>{BASIS_NOTE[basis]}</span>
                      ) : null}
                    </td>
                    <td>
                      <span className={styles.spanButtons}>
                        {detailSpans(turn).map((span) => (
                          <button
                            type="button"
                            key={`${span.trace_id}:${span.span_id}`}
                            className={styles.spanButton}
                            data-role={span.role}
                            data-error={span.status === "error" ? "true" : undefined}
                            aria-haspopup="dialog"
                            aria-expanded={openSpanId === span.span_id}
                            onClick={(event) => onOpenSpan(span, event.currentTarget)}
                          >
                            {spanButtonLabel(span)}
                          </button>
                        ))}
                        {detailSpans(turn).length === 0 ? (
                          <span className={styles.dim}>
                            {turn.spans.length === 0
                              ? "no spans"
                              : turn.spans.map((span) => humanize(span.role)).join(", ")}
                          </span>
                        ) : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
