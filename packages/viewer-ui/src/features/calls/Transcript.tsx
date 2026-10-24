import { useEffect, useId, useMemo, useState } from "react";
import type { PlacedLine } from "./api";
import { formatClock } from "./format";
import type { MediaClock } from "./mediaClock";
import styles from "./CallView.module.css";

interface TimedLine {
  lineNo: number;
  start: number;
  end: number | null;
}

// Speech overlaps (a backchannel inside a long turn), so the line under the
// playhead may have started a few lines before the latest one that has started.
const OVERLAP_LOOKBACK = 8;

/** The line under the playhead: of the lines that have started, the latest one
 * that has not ended. A start-only line lasts until a later line starts (its end
 * is unknown, and is never inferred from the next line); an order-only line is
 * never under the playhead. Binary search, then a short look back. */
function lineAt(timed: TimedLine[], ms: number): number | null {
  let low = 0;
  let high = timed.length - 1;
  let latest = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (timed[middle].start <= ms) {
      latest = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  for (let index = latest; index >= 0 && index > latest - OVERLAP_LOOKBACK; index--) {
    const line = timed[index];
    if (line.end == null ? index === latest : ms <= line.end) return line.lineNo;
  }
  return null;
}

/** The projection's transcript, placed on the call axis. The line under the
 * playhead is highlighted, and activating a timed line seeks the player there;
 * lines with no time (`order_only`) are listed in order and never seek. */
export function PlacedTranscript({
  lines,
  clock,
}: {
  lines: PlacedLine[];
  clock: MediaClock | null;
}) {
  const headingId = useId();
  const timed = useMemo(
    () =>
      lines
        .filter((line) => line.timing !== "order_only" && line.start_ms != null)
        .map((line) => ({
          lineNo: line.line_no,
          start: line.start_ms as number,
          end: line.end_ms,
        }))
        .sort((a, b) => a.start - b.start || a.lineNo - b.lineNo),
    [lines],
  );
  const [active, setActive] = useState<number | null>(null);

  useEffect(() => {
    if (clock == null) {
      setActive(null);
      return;
    }
    // Setting the same line again does not re-render: the list re-renders only
    // when the line under the playhead changes.
    const follow = (ms: number) => setActive(lineAt(timed, ms));
    follow(clock.currentTime);
    return clock.onTime(follow);
  }, [clock, timed]);

  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.panelTitle}>
        Transcript
      </h2>
      {lines.length === 0 ? (
        <p className={styles.note}>
          No transcript lines: the capture preset kept no text, or no speech was
          transcribed.
        </p>
      ) : (
        <ol className={styles.transcript}>
          {lines.map((line) => {
            const start = line.timing === "order_only" ? null : line.start_ms;
            const body = (
              <>
                <span className={styles.lineTime}>
                  {start == null ? "—" : formatClock(start)}
                </span>
                <span className={styles.speaker} data-speaker={line.speaker}>
                  {line.speaker}
                </span>
                <span className={styles.lineText}>{line.text}</span>
                {line.alignment_uncertain ? (
                  <span className={styles.lineFlag}>approximate placement</span>
                ) : null}
                {start == null ? (
                  <span className={styles.lineFlag}>not placed on the audio</span>
                ) : null}
              </>
            );
            return (
              <li
                key={line.line_no}
                className={styles.line}
                aria-current={active === line.line_no ? "time" : undefined}
              >
                {clock != null && start != null ? (
                  <button
                    type="button"
                    className={styles.lineButton}
                    onClick={() => clock.seek(start)}
                  >
                    {body}
                  </button>
                ) : (
                  <div className={styles.lineStatic}>{body}</div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
