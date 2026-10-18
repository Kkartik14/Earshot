import styles from "./EvidenceHandlingNote.module.css";

export function EvidenceHandlingNote() {
  return (
    <aside className={styles.note} aria-label="Evidence privacy and retention">
      <h2>Evidence, privacy, and retention</h2>
      <p>
        Capture is metadata-only by default. Transcript text, tool inputs and results, and
        sensitive diagnostics require an explicit capture-policy grant. Earshot does not
        ingest audio bytes; audio references are governed separately.
      </p>
      <p>
        The incident’s retention deadline controls how long its evidence is kept. Check
        the privacy manifest and omission ledger for what this call actually retained or
        withheld. Purging the incident also removes its Platform and Voice Labs links.
      </p>
    </aside>
  );
}
