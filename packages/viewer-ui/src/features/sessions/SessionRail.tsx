import { useIncidents, useLiveSessions } from "../../api/hooks";
import { Waveform } from "../../components/Waveform";
import { formatRelativeTime } from "../../lib/format";
import { statusTone } from "../../lib/status";
import { AnchorLink, type ViewerLinkComponent } from "../../navigation";
import styles from "./SessionRail.module.css";

export function SessionRail({
  currentPath = "/",
  LinkComponent = AnchorLink,
}: {
  currentPath?: string;
  LinkComponent?: ViewerLinkComponent;
}) {
  const incidents = useIncidents({ limit: 50 });
  const live = useLiveSessions();
  const items = incidents.data?.items ?? [];
  const liveItems = live.data?.items ?? [];

  return (
    <aside className={styles.rail}>
      <div className={styles.brand}>
        <Waveform className={styles.mark} />
        <b>earshot</b>
      </div>

      <LinkComponent
        href="/"
        className={
          currentPath === "/"
            ? `${styles.overview} ${styles.overviewActive}`
            : styles.overview
        }
      >
        Fleet metrics
      </LinkComponent>

      {live.isError ? (
        <p className={styles.note} role="alert">
          Active sessions are unavailable. Refresh this page to try again.
        </p>
      ) : null}

      {liveItems.length > 0 ? (
        <>
          <span className={styles.eyebrow}>In progress</span>
          <nav className={styles.list} aria-label="Sessions in progress">
            {liveItems.map((item) => (
              <LinkComponent
                key={item.session_id}
                href={`/live/${encodeURIComponent(item.session_id)}`}
                className={
                  currentPath === `/live/${encodeURIComponent(item.session_id)}`
                    ? `${styles.row} ${styles.active}`
                    : styles.row
                }
              >
                <span className={styles.top}>
                  <span className={`${styles.dot} ${styles.warn}`} />
                  <span className={styles.id}>{item.session_id}</span>
                  {/* Never a plain "live" chip: what matters is that it is not
                      a finished account of anything. */}
                  <span className={`${styles.badge} ${styles.liveBadge}`}>
                    incomplete
                  </span>
                </span>
                <span className={styles.meta}>
                  {item.state} · record #{item.last_sequence}
                </span>
              </LinkComponent>
            ))}
          </nav>
        </>
      ) : null}

      <span className={styles.eyebrow}>Sessions</span>

      <nav className={styles.list}>
        {incidents.isPending ? <p className={styles.note}>Loading…</p> : null}
        {incidents.isError ? <p className={styles.note}>Backend unavailable</p> : null}
        {incidents.isSuccess && items.length === 0 ? (
          <p className={styles.note}>No sessions yet</p>
        ) : null}

        {items.map((item) => (
          <LinkComponent
            key={item.bundle_id}
            href={`/sessions/${encodeURIComponent(item.bundle_id)}`}
            className={
              currentPath === `/sessions/${encodeURIComponent(item.bundle_id)}`
                ? `${styles.row} ${styles.active}`
                : styles.row
            }
          >
            <span className={styles.top}>
              <span className={`${styles.dot} ${styles[statusTone(item.status)]}`} />
              <span className={styles.id}>{item.session_id}</span>
              {item.finality === "final" ? null : (
                // The producer never had the last word on this one; a fleet
                // reader must see that before comparing it with anything.
                <span className={styles.badge}>{item.finality}</span>
              )}
            </span>
            <span className={styles.meta}>
              {item.framework ?? "custom"} ·{" "}
              {formatRelativeTime(item.ingested_at_unix_nano)}
            </span>
          </LinkComponent>
        ))}
      </nav>

      <div className={styles.foot}>
        <Waveform className={styles.footMark} />
        v0.1 · self-hosted
      </div>
    </aside>
  );
}
