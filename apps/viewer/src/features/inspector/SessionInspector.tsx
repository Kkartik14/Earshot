import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useAnalysis, useIncident } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionHeader } from "./SessionHeader";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline } from "./TurnTimeline";
import styles from "./SessionInspector.module.css";
import {
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  getCoverage,
  type AnalysisLike,
  type IncidentLike,
} from "./timeline";

export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  const incident = useIncident(bundleId);
  const analysis = useAnalysis(bundleId);
  const [selected, setSelected] = useState<number | null>(null);

  // A different session resets the open drawer.
  useEffect(() => setSelected(null), [bundleId]);
  useEffect(() => {
    if (selected == null) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelected(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  if (incident.isPending || analysis.isPending) {
    return <EmptyState title="Loading session…" />;
  }
  if (!incident.data || !analysis.data) {
    return (
      <EmptyState
        title="Couldn't load this session"
        hint="The backend may be unavailable, or this incident has no derived analysis yet."
      />
    );
  }

  // The API responses are the source of truth; the transform reads only the
  // fields it needs, so we narrow to the local shapes at this boundary.
  const inc = incident.data as unknown as IncidentLike;
  const derived = analysis.data.analysis as unknown as AnalysisLike;
  const timeline = buildTimeline(inc, derived);
  const summary = buildSummary(inc, timeline);
  const details = buildTurnDetails(inc, derived);
  const coverage = getCoverage(inc);
  const open = selected != null && details[selected] != null;

  return (
    <div className={styles.inspector} data-open={open ? "" : undefined}>
      <div className={styles.main}>
        <SessionHeader summary={summary} />
        <TurnTimeline
          timeline={timeline}
          selectedIndex={selected}
          onSelect={(index) => setSelected((cur) => (cur === index ? null : index))}
        />
      </div>
      {open ? (
        <div className={styles.drawerCol}>
          <TurnDrawer
            detail={details[selected]}
            coverage={coverage}
            onClose={() => setSelected(null)}
          />
        </div>
      ) : null}
    </div>
  );
}
