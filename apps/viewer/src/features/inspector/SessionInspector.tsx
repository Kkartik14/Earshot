import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useExplanation, useIncident } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionHeader } from "./SessionHeader";
import { DiagnosesPanel, UnassignedPanel } from "./SessionFacts";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline, type Selection } from "./TurnTimeline";
import styles from "./SessionInspector.module.css";
import {
  buildDiagnoses,
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  buildUnassigned,
  getCoverage,
  type ExplanationLike,
  type IncidentLike,
} from "./timeline";

export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  const incident = useIncident(bundleId);
  const explanation = useExplanation(bundleId);
  const [openTurns, setOpenTurns] = useState<Set<number>>(new Set());
  const [selection, setSelection] = useState<Selection | null>(null);
  // The control that opened the detail dialog; focus returns here on close.
  const restoreFocus = useRef<HTMLElement | null>(null);
  const initializedExplanationFor = useRef<string | undefined>(undefined);

  // A session change invalidates both the current detail and its invoking
  // control. A same-session data refresh must preserve them.
  useEffect(() => {
    setSelection(null);
    restoreFocus.current = null;
    initializedExplanationFor.current = undefined;
    setOpenTurns(new Set());
  }, [bundleId]);

  // Open the slow turn once when this session's explanation first arrives.
  // Background refreshes update the open detail without closing it or
  // disturbing the user's expansion state.
  useEffect(() => {
    if (explanation.data == null || initializedExplanationFor.current === bundleId) {
      return;
    }
    initializedExplanationFor.current = bundleId;
    const turns =
      (explanation.data as unknown as ExplanationLike | undefined)?.turns ?? [];
    const slow = turns.findIndex(
      (t) => (t.metrics?.first_token_latency?.value ?? 0) > 500,
    );
    setOpenTurns(slow >= 0 ? new Set([slow]) : new Set());
  }, [bundleId, explanation.data]);

  useEffect(() => {
    if (selection == null) {
      // Every close path lands here; return focus to the invoking control.
      const trigger = restoreFocus.current;
      restoreFocus.current = null;
      if (trigger?.isConnected) trigger.focus();
      return;
    }
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelection(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selection]);

  if (incident.isPending || explanation.isPending) {
    return <EmptyState title="Loading session…" />;
  }
  if (!incident.data || !explanation.data) {
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
  const explained = explanation.data as unknown as ExplanationLike;
  const timeline = buildTimeline(explained);
  const summary = buildSummary(inc, explained, timeline);
  const details = buildTurnDetails(explained);
  const coverage = getCoverage(explained);
  const diagnoses = buildDiagnoses(explained);
  const unassigned = buildUnassigned(explained);

  const openTurn = (i: number) =>
    setOpenTurns((prev) => (prev.has(i) ? prev : new Set(prev).add(i)));
  // Capture the invoking control only on the first open; switching turns/stages
  // while the dialog is already open must not overwrite the restore target.
  const rememberTrigger = () => {
    if (selection == null)
      restoreFocus.current = document.activeElement as HTMLElement | null;
  };
  const toggleTurn = (i: number) => {
    rememberTrigger();
    setOpenTurns((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
    setSelection({ turn: i, operationId: null });
  };
  const selectOperation = (i: number, operationId: string) => {
    rememberTrigger();
    openTurn(i);
    setSelection({ turn: i, operationId });
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
          onSelectOperation={selectOperation}
        />
        <DiagnosesPanel diagnoses={diagnoses} onSelectEvidence={selectOperation} />
        <UnassignedPanel facts={unassigned} />
      </div>
      {sel ? (
        <div className={styles.drawerCol}>
          {sel.operationId == null ? (
            <TurnDrawer
              detail={details[sel.turn]}
              coverage={coverage}
              onClose={() => setSelection(null)}
              onPickStage={(operationId) => selectOperation(sel.turn, operationId)}
            />
          ) : (
            (() => {
              const op = details[sel.turn].stages.find(
                (s) => s.operationId === sel.operationId,
              );
              return op == null ? (
                <TurnDrawer
                  detail={details[sel.turn]}
                  coverage={coverage}
                  onClose={() => setSelection(null)}
                  onPickStage={(operationId) => selectOperation(sel.turn, operationId)}
                />
              ) : (
                <StageDrawer
                  index={sel.turn}
                  stage={op}
                  onClose={() => setSelection(null)}
                />
              );
            })()
          )}
        </div>
      ) : null}
    </div>
  );
}
