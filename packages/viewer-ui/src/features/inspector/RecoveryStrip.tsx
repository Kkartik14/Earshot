import type { IncidentLike } from "./timeline";
import styles from "./RecoveryStrip.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

type Recovery = NonNullable<NonNullable<IncidentLike["profile"]["manifest"]>["recovery"]>;

/** Persistent notice that this artifact is not a clean, complete close.
 *
 *  It is rendered from `manifest.recovery` — the typed declaration validation
 *  already forces a non-final bundle to carry — so it cannot be forgotten by a
 *  producer or dropped by a viewer that renders the incident at all. Two sources
 *  reach this state: a checkpoint-journal replay after the process died, and a
 *  browser capture batch, which is a partial observation of a session still in
 *  progress. Both never saw the close; the copy states which, and never claims a
 *  journal a journal-less reconstruction (a capture batch) never had. */
export function RecoveryStrip({ incident }: { incident: IncidentLike }) {
  const manifest = incident.profile.manifest;
  const recovery = manifest?.recovery as Recovery | null | undefined;
  const finality = manifest?.finality ?? "final";
  if (recovery == null && finality === "final") return null;

  const journalId = recovery?.journal_id ?? null;
  const fromJournal = journalId != null;

  return (
    <section className={styles.strip} aria-label="Recovered artifact">
      <p className={styles.headline} role="status">
        <span className={styles.tag}>
          {fromJournal ? "RECOVERED" : "PARTIAL CAPTURE"} — NOT A CLEAN CLOSE
        </span>
        <span className={styles.explain}>
          {fromJournal
            ? "The process ended before this session was closed, so this artifact was reconstructed from the recorder’s checkpoint journal"
            : "This artifact is a partial observation of a session still in progress; the browser drained telemetry mid-call and never observed the close"}
          {recovery == null ? "" : ` (${humanize(recovery.reason)})`}. It is {finality}{" "}
          and incomplete: the session&rsquo;s real end was never observed and is absent
          rather than estimated.
        </span>
      </p>
      {recovery == null ? null : (
        <ul className={styles.facts}>
          <li>
            close observed: <strong>{recovery.close_observed ? "yes" : "no"}</strong>
          </li>
          {journalId == null ? null : (
            <li>
              journal {journalId.slice(0, 8)} through record {recovery.last_sequence}
            </li>
          )}
          {recovery.torn_tail_bytes > 0 ? (
            <li className={styles.loss}>
              evidence was lost at the end of the journal ({recovery.torn_tail_bytes}{" "}
              bytes)
            </li>
          ) : null}
          {recovery.journal_complete === false && fromJournal ? (
            <li className={styles.loss}>
              the journal reached its cap, so later facts were never written
            </li>
          ) : null}
        </ul>
      )}
    </section>
  );
}
