import { useSessionIncidents, useLiveSessions } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionInspector } from "../inspector/SessionInspector";
import { LiveSessionView } from "../live/LiveSessionView";
import { AnchorLink, type ViewerLinkComponent } from "../../navigation";
import styles from "./SessionResolver.module.css";

export function SessionResolver({
  sessionId,
  LinkComponent = AnchorLink,
  hrefForReference,
}: {
  sessionId: string;
  LinkComponent?: ViewerLinkComponent;
  hrefForReference?: Parameters<typeof SessionInspector>[0]["hrefForReference"];
}) {
  const incidents = useSessionIncidents(sessionId, { enabled: true });
  const records = incidents.data?.pages.flatMap((page) => page.items) ?? [];
  const hasMoreRecords = incidents.hasNextPage;
  const live = useLiveSessions({
    enabled:
      records.length === 0 &&
      !incidents.isPending &&
      !incidents.isError &&
      !hasMoreRecords,
  });

  if (incidents.isError && records.length === 0) {
    return (
      <div role="alert">
        <EmptyState
          title="Couldn't find this session's stored evidence"
          hint="The Earshot service may be unavailable. Try again before treating the session as missing."
        />
      </div>
    );
  }

  if (records.length > 1 || hasMoreRecords) {
    return (
      <section className={styles.chooser} aria-labelledby="session-records-title">
        <header className={styles.header}>
          <h1 id="session-records-title">More than one artifact uses this session ID</h1>
          <p>Choose the immutable evidence record you want to inspect.</p>
        </header>
        <ul className={styles.list}>
          {records.map((record) => (
            <li key={record.bundle_id}>
              <LinkComponent
                href={`/sessions/${encodeURIComponent(record.bundle_id)}`}
                className={styles.link}
              >
                <span className={styles.title}>
                  {record.framework ?? "Voice"} session
                </span>
                <span className={styles.meta}>
                  {record.status} · {record.finality} · {record.bundle_id}
                </span>
              </LinkComponent>
            </li>
          ))}
        </ul>
        {incidents.isFetchNextPageError ? (
          <p role="alert" className={styles.error}>
            More artifacts could not be loaded. The artifacts already found remain
            available; retry to check for additional matches.
          </p>
        ) : null}
        {hasMoreRecords ? (
          <button
            className={`${styles.link} ${styles.more}`}
            type="button"
            disabled={incidents.isFetchingNextPage}
            onClick={() => void incidents.fetchNextPage()}
          >
            {incidents.isFetchingNextPage
              ? "Loading more artifacts…"
              : incidents.isFetchNextPageError
                ? "Retry loading more artifacts"
                : "Load more artifacts"}
          </button>
        ) : null}
      </section>
    );
  }

  if (records.length === 1) {
    return (
      <SessionInspector
        bundleId={records[0].bundle_id}
        hrefForReference={hrefForReference}
      />
    );
  }

  if (incidents.isPending || live.isPending) {
    return <EmptyState title="Finding this session…" />;
  }

  if (live.isError) {
    return (
      <div role="alert">
        <EmptyState
          title="Couldn't check for an in-progress session"
          hint="The Earshot service may be unavailable. Try again before treating the session as missing."
        />
      </div>
    );
  }

  if (live.data?.items.some((item) => item.session_id === sessionId)) {
    return <LiveSessionView sessionId={sessionId} LinkComponent={LinkComponent} />;
  }

  return (
    <EmptyState
      title="No Earshot evidence is available for this session right now"
      hint="This does not show whether the call completed or failed. Evidence may not have arrived, may have expired, or may not be retained for this project."
    />
  );
}
