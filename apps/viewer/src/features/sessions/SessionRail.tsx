import { NavLink } from "react-router-dom";
import { useIncidents } from "../../api/hooks";
import { Waveform } from "../../components/Waveform";
import { formatRelativeTime } from "../../lib/format";
import { statusTone } from "../../lib/status";
import styles from "./SessionRail.module.css";

export function SessionRail() {
  const incidents = useIncidents({ limit: 50 });
  const items = incidents.data?.items ?? [];

  return (
    <aside className={styles.rail}>
      <div className={styles.brand}>
        <Waveform className={styles.mark} />
        <b>earshot</b>
      </div>
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
        v1 · self-hosted
      </div>
    </aside>
  );
}
