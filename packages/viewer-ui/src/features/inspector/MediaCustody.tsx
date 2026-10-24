import { formatDuration } from "../../lib/format";
import type { MediaCustodyView, MediaRetentionView } from "./timeline";
import styles from "./MediaCustody.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

const retentionText = (retention: MediaRetentionView | null): string => {
  if (retention == null) return "no retention policy declared";
  if (retention.expiresAtUnixNano != null) {
    const ms = Number(BigInt(retention.expiresAtUnixNano) / 1_000_000n);
    return `expires ${new Date(ms).toISOString().replace("T", " ").slice(0, 19)}Z`;
  }
  if (retention.ttlMs != null) return `ttl ${formatDuration(retention.ttlMs)}`;
  return retention.policyId ?? "retention policy declared without terms";
};

const alignmentText = (media: MediaCustodyView): string => {
  const alignment = media.alignment;
  if (alignment.state !== "aligned") return alignment.note;
  const bound =
    alignment.uncertaintyMs == null
      ? // The calibration declares no error bound. Unknown is not zero, and
        // rendering "±0ms" would invent a precision nobody claimed.
        "uncertainty not declared"
      : `±${formatDuration(alignment.uncertaintyMs)}`;
  const drift =
    alignment.driftPpm == null || alignment.driftPpm === 0
      ? ""
      : ` · drift ${alignment.driftPpm} ppm`;
  return `${humanize(alignment.method)} ${bound}${drift} · ${alignment.note}`;
};

const ALIGNMENT_LABEL: Record<MediaCustodyView["alignment"]["state"], string> = {
  session_domain: "same clock domain",
  aligned: "aligned",
  unaligned: "cannot align",
  undeclared: "no media timeline",
};

/** The recording Earshot itself holds (ADR 0004): the call materializer names
 *  this custodian on every recording it stored, with the digest it measured. */
const EARSHOT_CUSTODIAN = "earshot.blob_store";
const heldByEarshot = (media: MediaCustodyView) => media.custodian === EARSHOT_CUSTODIAN;

const headNote = (media: MediaCustodyView[]): string => {
  const held = media.filter(heldByEarshot).length;
  if (held === media.length) return "held by earshot · played in the call view, not here";
  if (held === 0) return "held elsewhere · earshot stores the reference, never the media";
  return "some held by earshot, some elsewhere · this panel loads no media";
};

/** Custody facts for the media an incident references. The panel reports custody;
 *  it never plays or loads media.
 *
 *  Since ADR 0004 there are two kinds of reference, and the copy keeps them apart:
 *
 *  1. A recording Earshot holds (custodian `earshot.blob_store`). Earshot stored the
 *     bytes and measured their sha256 while storing them, so the integrity is its
 *     own measurement. The call view plays it, streamed through the server with
 *     Range requests; this panel only says so.
 *  2. Media somebody else holds. Earshot never fetches, caches or proxies it, and
 *     integrity is attributed to whoever declared the digest. The locator is a
 *     plain user-initiated link with `rel="noreferrer"`, never an `<audio src>`:
 *     an `src` would fetch the media on render, leaking the reader's network
 *     position and the referrer to the custodian without anyone asking for
 *     playback. Following the link is the reader's own request, with the
 *     reader's own credentials, and nothing about it passes through Earshot. */
export function MediaCustodyPanel({ media }: { media: MediaCustodyView[] }) {
  if (media.length === 0) return null;
  return (
    <section className={styles.panel} aria-label="Media custody">
      <div className={styles.panelHead}>
        <h2>Media custody</h2>
        <span className={styles.count}>{media.length}</span>
        <span className={styles.note}>{headNote(media)}</span>
      </div>

      <div className={styles.list}>
        {media.map((item) => (
          <article key={item.mediaId} className={styles.item}>
            <div className={styles.itemHead}>
              <span className={styles.name}>{item.mediaId}</span>
              <span className={styles.kind}>
                {item.mediaKind} · {item.contentType}
              </span>
              <span
                className={
                  item.integrity === "content_digest"
                    ? styles.integrityDigest
                    : styles.integrityOpaque
                }
              >
                {humanize(item.integrity)}
              </span>
            </div>

            <div className={styles.rows}>
              <div className={styles.row}>
                <span className={styles.label}>custodian</span>
                <span className={styles.value}>{item.custodian ?? "not declared"}</span>
                <span className={styles.detail}>
                  {item.custodian == null
                    ? "nobody is named as holding these bytes"
                    : heldByEarshot(item)
                      ? "earshot holds these bytes in its blob store"
                      : "holds the bytes; earshot does not"}
                </span>
              </div>

              <div className={styles.row}>
                <span className={styles.label}>integrity</span>
                <span className={styles.value}>
                  {item.digest == null
                    ? "no digest"
                    : `sha256 ${item.digest.slice(0, 12)}…`}
                </span>
                <span className={styles.detail}>
                  {heldByEarshot(item) && item.digest != null
                    ? "sha256 measured by earshot while storing the recording"
                    : item.integrityNote}
                </span>
              </div>

              <div className={styles.row}>
                <span className={styles.label}>alignment</span>
                <span
                  className={
                    item.alignment.state === "aligned" ||
                    item.alignment.state === "session_domain"
                      ? styles.value
                      : `${styles.value} ${styles.dim}`
                  }
                >
                  {ALIGNMENT_LABEL[item.alignment.state]}
                </span>
                <span className={styles.detail}>{alignmentText(item)}</span>
              </div>

              <div className={styles.row}>
                <span className={styles.label}>covers</span>
                <span className={styles.value}>
                  {item.coveredMs == null ? "—" : formatDuration(item.coveredMs)}
                </span>
                <span className={styles.detail}>{item.coveredNote}</span>
              </div>

              <div className={styles.row}>
                <span className={styles.label}>governance</span>
                <span className={styles.value}>
                  {item.consent == null ? "consent not declared" : humanize(item.consent)}
                </span>
                <span className={styles.detail}>{retentionText(item.retention)}</span>
              </div>
            </div>

            {heldByEarshot(item) ? (
              <p className={styles.handoff}>
                <span className={styles.handoffNote}>
                  This is a call recording earshot stored. Play it from the call&apos;s
                  view (Calls), which streams it from this server; this panel loads none
                  of it.
                </span>
              </p>
            ) : item.locatorUri == null ? (
              <p className={styles.noLocator}>
                No locator is declared, so there is nowhere for a reader to go for these
                bytes.
              </p>
            ) : (
              <p className={styles.handoff}>
                <a
                  className={styles.link}
                  href={item.locatorUri}
                  target="_blank"
                  rel="noreferrer noopener external"
                  referrerPolicy="no-referrer"
                >
                  Open at the custodian
                </a>
                <span className={styles.handoffNote}>
                  Opens a direct request from your browser to the custodian, with your own
                  credentials. Earshot does not fetch, proxy, cache, or play this media,
                  and no part of it is loaded on this page.
                </span>
              </p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
