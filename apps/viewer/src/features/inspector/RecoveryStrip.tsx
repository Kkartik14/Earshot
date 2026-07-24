import type { IncidentLike } from "./timeline";
import styles from "./RecoveryStrip.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

type Recovery = NonNullable<NonNullable<IncidentLike["profile"]["manifest"]>["recovery"]>;

/** Persistent notice that this artifact was reconstructed rather than closed.
 *
 *  It is rendered from `manifest.recovery` — the typed declaration validation
 *  already forces a reconstructed bundle to carry — so it cannot be forgotten by
 *  a producer or dropped by a viewer that renders the incident at all. */
export function RecoveryStrip({ incident }: { incident: IncidentLike }) {
  const manifest = incident.profile.manifest;
  const recovery = manifest?.recovery as Recovery | null | undefined;
  const finality = manifest?.finality ?? "final";
  if (recovery == null && finality === "final") return null;

  return (
    <section className={styles.strip} aria-label="Recovered artifact">
      <p className={styles.headline} role="status">
        <span className={styles.tag}>RECOVERED — NOT A CLEAN CLOSE</span>
        <span className={styles.explain}>
          The process ended before this session was closed, so this artifact was
          reconstructed from the recorder&rsquo;s checkpoint journal
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
          <li>
            journal {recovery.journal_id.slice(0, 8)} through record{" "}
            {recovery.last_sequence}
          </li>
          {recovery.torn_tail_bytes > 0 ? (
            <li className={styles.loss}>
              evidence was lost at the end of the journal ({recovery.torn_tail_bytes}{" "}
              bytes)
            </li>
          ) : null}
          {recovery.journal_complete === false ? (
            <li className={styles.loss}>
              the journal reached its cap, so later facts were never written
            </li>
          ) : null}
        </ul>
      )}
    </section>
  );
}
