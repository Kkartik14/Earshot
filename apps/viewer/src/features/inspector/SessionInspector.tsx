import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useAnalysis, useIncident } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionHeader } from "./SessionHeader";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline, type Selection } from "./TurnTimeline";
import styles from "./SessionInspector.module.css";
import {
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  getCoverage,
  type AnalysisLike,
  type IncidentLike,
  type StageName,
} from "./timeline";

export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  const incident = useIncident(bundleId);
  const analysis = useAnalysis(bundleId);
  const [openTurns, setOpenTurns] = useState<Set<number>>(new Set());
  const [selection, setSelection] = useState<Selection | null>(null);

  // Switching sessions resets selection and opens the slow turn, so the
  // expandable breakdown is visible the moment a flagged session loads.
  useEffect(() => {
    setSelection(null);
    const turns =
      (analysis.data?.analysis as unknown as AnalysisLike | undefined)?.projections
        ?.turns ?? [];
    const slow = turns.findIndex(
      (t) => (t.metrics?.first_token_latency?.value ?? 0) > 500,
    );
    setOpenTurns(slow >= 0 ? new Set([slow]) : new Set());
  }, [bundleId, analysis.data]);

  useEffect(() => {
    if (selection == null) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelection(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selection]);

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

  const openTurn = (i: number) =>
    setOpenTurns((prev) => (prev.has(i) ? prev : new Set(prev).add(i)));
  const toggleTurn = (i: number) => {
    setOpenTurns((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
    setSelection({ turn: i, stage: null });
  };
  const selectStage = (i: number, stage: StageName) => {
    openTurn(i);
    setSelection({ turn: i, stage });
  };

  const sel = selection != null && details[selection.turn] != null ? selection : null;

  return (
    <div className={styles.inspector} data-open={sel ? "" : undefined}>
      <div className={styles.main}>
        <SessionHeader summary={summary} />
        <TurnTimeline
          timeline={timeline}
          openTurns={openTurns}
          selection={sel}
          onToggleTurn={toggleTurn}
          onSelectStage={selectStage}
        />
      </div>
      {sel ? (
        <div className={styles.drawerCol}>
          {sel.stage == null ? (
            <TurnDrawer
              detail={details[sel.turn]}
              coverage={coverage}
              onClose={() => setSelection(null)}
              onPickStage={(stage) => selectStage(sel.turn, stage)}
            />
          ) : (
            <StageDrawer
              index={sel.turn}
              stage={details[sel.turn].stages.find((s) => s.name === sel.stage)!}
              onClose={() => setSelection(null)}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}
