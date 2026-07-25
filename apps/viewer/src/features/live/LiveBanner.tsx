import type { TailConnection } from "../../api/tail";
import type { LiveFacts } from "./liveStore";
import styles from "./LiveBanner.module.css";

/** The one-line truth about what is being shown. Never decorative.
 *
 *  This is the discriminator between "an incident" and "a conversation still
 *  being written", and it is structural: the live view renders it above
 *  everything, always, and describes itself by it. */
export type LiveStanding =
  | "in_progress"
  | "stalled"
  | "closed_awaiting_artifact"
  | "ended_without_close"
  | "disconnected";

export function standingOf(facts: LiveFacts, connection: TailConnection): LiveStanding {
  if (facts.closeObserved) return "closed_awaiting_artifact";
  if (facts.ending != null) return "ended_without_close";
  if (connection.kind === "closed") return "disconnected";
  if (connection.kind === "stale") return "stalled";
  return "in_progress";
}

const HEADLINE: Record<LiveStanding, string> = {
  in_progress: "LIVE — INCOMPLETE",
  stalled: "LIVE — INCOMPLETE · NOT RESPONDING",
  closed_awaiting_artifact: "CLOSED — ARTIFACT NOT YET STORED",
  ended_without_close: "STREAM ENDED — NO CLOSE RECORD",
  disconnected: "DISCONNECTED — INCOMPLETE",
};

const EXPLANATION: Record<LiveStanding, string> = {
  in_progress:
    "This conversation has not closed. Everything below is what the recorder has " +
    "durably admitted so far, and nothing more; facts that have not happened yet are " +
    "indistinguishable here from facts that never will.",
  stalled:
    "No fact and no heartbeat has arrived recently. The session may still be running, " +
    "or the connection may be broken — this view cannot tell which.",
  closed_awaiting_artifact:
    "The recorder closed. The immutable artifact is delivered separately and is not " +
    "on this stream; until it is stored, this remains a partial account.",
  ended_without_close:
    "The stream ended without a close record. This session's evidence may be " +
    "incomplete, and how incomplete is not knowable from here.",
  disconnected:
    "This tail is no longer connected. What is shown stops at the last fact that " +
    "arrived; later facts may exist and are not shown.",
};

const TONE: Record<LiveStanding, string> = {
  in_progress: styles.live,
  stalled: styles.warn,
  closed_awaiting_artifact: styles.warn,
  ended_without_close: styles.crit,
  disconnected: styles.crit,
};

function ageLabel(silentForMs: number): string {
  const seconds = Math.floor(silentForMs / 1000);
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s ago`;
}

export function LiveBanner({
  facts,
  connection,
  silentForMs,
  headingId,
}: {
  facts: LiveFacts;
  connection: TailConnection;
  silentForMs: number;
  headingId: string;
}) {
  const standing = standingOf(facts, connection);
  return (
    <section
      className={`${styles.banner} ${TONE[standing]}`}
      aria-label="Live session status"
    >
      {/* Only the discrete standing is announced. The as-of line ticks every
          second and would otherwise flood a screen reader with noise. */}
      <p className={styles.headline} role="status" id={headingId}>
        <span className={styles.tag}>{HEADLINE[standing]}</span>
        <span className={styles.explain}>{EXPLANATION[standing]}</span>
      </p>
      <p className={styles.asOf}>
        Facts as of journal record #{facts.asOfSequence}
        {facts.journalId == null ? "" : ` of journal ${facts.journalId.slice(0, 8)}`},
        last heard {ageLabel(silentForMs)}.
      </p>
      {facts.truncation != null ? (
        <p className={styles.asOf}>
          {facts.truncation.withheldRecords} earlier record
          {facts.truncation.withheldRecords === 1 ? "" : "s"} exist in this session and
          are not shown here ({facts.truncation.reason.replace(/_/g, " ")}).
        </p>
      ) : null}
      {facts.journalExhausted ? (
        <p className={styles.asOf}>
          The recorder&rsquo;s journal reached its cap. Facts admitted after that point
          were not written and cannot appear here or in the artifact.
        </p>
      ) : null}
      {facts.overflowed ? (
        <p className={styles.asOf}>
          This connection fell behind and the server closed it rather than dropping
          records. Reconnecting resumes exactly where it stopped.
        </p>
      ) : null}
    </section>
  );
}
