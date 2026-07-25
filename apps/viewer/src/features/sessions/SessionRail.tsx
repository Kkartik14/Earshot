import { NavLink } from "react-router-dom";
import { useIncidents, useLiveSessions } from "../../api/hooks";
import { Waveform } from "../../components/Waveform";
import { formatRelativeTime } from "../../lib/format";
import { statusTone } from "../../lib/status";
import styles from "./SessionRail.module.css";

export function SessionRail() {
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

      <NavLink
        to="/"
        end
        className={({ isActive }) =>
          isActive ? `${styles.overview} ${styles.overviewActive}` : styles.overview
        }
      >
        Fleet metrics
      </NavLink>

      {liveItems.length > 0 ? (
        <>
          <span className={styles.eyebrow}>In progress</span>
          <nav className={styles.list} aria-label="Sessions in progress">
            {liveItems.map((item) => (
              <NavLink
                key={item.session_id}
                to={`/live/${encodeURIComponent(item.session_id)}`}
                className={({ isActive }) =>
                  isActive ? `${styles.row} ${styles.active}` : styles.row
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
              </NavLink>
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
          <NavLink
            key={item.bundle_id}
            to={`/sessions/${encodeURIComponent(item.bundle_id)}`}
            className={({ isActive }) =>
              isActive ? `${styles.row} ${styles.active}` : styles.row
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
          </NavLink>
        ))}
      </nav>

      <div className={styles.foot}>
        <Waveform className={styles.footMark} />
        v0.1 · self-hosted
      </div>
    </aside>
  );
}
