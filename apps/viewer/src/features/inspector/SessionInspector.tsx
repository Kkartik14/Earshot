import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useContradictions, useExplanation, useIncident } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { SessionHeader } from "./SessionHeader";
import {
  ClockCalibrationPanel,
  ContradictionsPanel,
  DiagnosesPanel,
  UnassignedPanel,
  contradictionsReason,
  type ContradictionsStatus,
} from "./SessionFacts";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline, type Selection } from "./TurnTimeline";
import styles from "./SessionInspector.module.css";
import {
  buildClockCalibration,
  buildContradictions,
  buildDiagnoses,
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  buildUnassigned,
  getCoverage,
} from "./timeline";

export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  const incident = useIncident(bundleId);
  const explanation = useExplanation(bundleId);
  const contradictions = useContradictions(bundleId);
  const [openTurns, setOpenTurns] = useState<Set<number>>(new Set());
  const [selection, setSelection] = useState<Selection | null>(null);
  // The control that opened the detail dialog; focus returns here on close.
  const restoreFocus = useRef<HTMLElement | null>(null);

  // A session change invalidates both the current detail and its invoking
  // control. A same-session data refresh must preserve them.
  useEffect(() => {
    setSelection(null);
    restoreFocus.current = null;
    setOpenTurns(new Set());
  }, [bundleId]);

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

  // Both responses flow directly from the generated API contract.
  const inc = incident.data;
  const explained = explanation.data;
  const timeline = buildTimeline(explained);
  const summary = buildSummary(inc, explained, timeline);
  const details = buildTurnDetails(explained);
  const coverage = getCoverage(explained);
  const diagnoses = buildDiagnoses(explained);
  const unassigned = buildUnassigned(explained);
  const calibration = buildClockCalibration(inc, details);
  // A detection that has not answered, or could not run, is reported as such.
  // Only a resolved report may be read as "these are the conflicts".
  const contradictionsStatus: ContradictionsStatus = contradictions.data
    ? "ready"
    : contradictions.isPending
      ? "pending"
      : "unavailable";
  const contradictionsUnavailable =
    contradictionsStatus === "unavailable"
      ? contradictionsReason(contradictions.error)
      : null;
  const contradictionViews = contradictions.data
    ? buildContradictions(explained, contradictions.data)
    : [];

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
        <ContradictionsPanel
          status={contradictionsStatus}
          reason={contradictionsUnavailable}
          contradictions={contradictionViews}
          onSelectEvidence={selectOperation}
        />
        <ClockCalibrationPanel calibration={calibration} />
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
