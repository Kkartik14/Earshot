import { useParams } from "react-router-dom";
import { useAnalysis, useIncident } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionHeader } from "./SessionHeader";
import { TurnTimeline } from "./TurnTimeline";
import {
  buildSummary,
  buildTimeline,
  type AnalysisLike,
  type IncidentLike,
} from "./timeline";

export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  const incident = useIncident(bundleId);
  const analysis = useAnalysis(bundleId);

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

  return (
    <div>
      <SessionHeader summary={summary} />
      <TurnTimeline timeline={timeline} />
    </div>
  );
}
