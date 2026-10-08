import { type CSSProperties } from "react";
import { formatMs } from "../../lib/format";
import { toneColorVar } from "../../lib/status";
import styles from "./TurnTimeline.module.css";
import {
  roleColorVar,
  roleLabel,
  type OperationRole,
  type StageBar,
  type Timeline,
} from "./timeline";

const TICK_COUNT = 6;

export interface Selection {
  turn: number;
  operationId: string | null;
}

/** The role sub-label shown when an operation carries no provider/model. */
function operationSubtitle(stage: StageBar): string {
  if (stage.provider != null || stage.model != null) {
    return `${stage.provider ?? "?"} · ${stage.model ?? "?"}`;
  }
  return roleLabel(stage.role);
}

/** Faithful right-column readout: lead when known, else the observed interval,
 * else the bare shape. Never a fabricated number. */
function stageReadout(stage: StageBar): string {
  if (stage.leadMs != null) return formatMs(stage.leadMs);
  if (stage.timing === "interval" && stage.startMs != null && stage.endMs != null) {
    return formatMs(stage.endMs - stage.startMs);
  }
  if (stage.timing === "point") return "point";
  return "not observed";
}

function Bar({ stage, scale }: { stage: StageBar; scale: number }) {
  const color = { "--c": roleColorVar(stage.role) } as CSSProperties;
  if (stage.startMs == null) {
    return <div className={styles.unplaced} title={`${stage.name} timing unavailable`} />;
  }
  if (stage.timing === "point" || stage.endMs == null) {
    return (
      <div
        className={styles.point}
        style={{ ...color, left: `${(stage.startMs / scale) * 100}%` }}
        title={`${stage.name} point · ${stage.provider ?? "?"} · interval not observed`}
      />
    );
  }
  const width = stage.endMs - stage.startMs;
  const leadPct =
    width > 0 && stage.leadMs != null ? Math.min(100, (stage.leadMs / width) * 100) : 0;
  return (
    <div
      className={styles.bar}
      style={{
        ...color,
        left: `${(stage.startMs / scale) * 100}%`,
        width: `${(width / scale) * 100}%`,
      }}
      title={`${stage.name} observed interval · ${stage.provider ?? "?"}`}
    >
      <div className={styles.tail} />
      <div className={styles.lead} style={{ width: `${leadPct}%` }} />
    </div>
  );
}

function Caret({ open }: { open: boolean }) {
  return (
    <span className={`${styles.caret} ${open ? styles.caretOpen : ""}`}>
      <svg viewBox="0 0 10 10" fill="none" aria-hidden="true">
        <path
          d="M3 1 L7 5 L3 9"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

function TurnRow({
  turn,
  scale,
  open,
  selection,
  onToggle,
  onOperation,
}: {
  turn: Timeline["turns"][number];
  scale: number;
  open: boolean;
  selection: Selection | null;
  onToggle: (index: number) => void;
  onOperation: (index: number, operationId: string) => void;
}) {
  const llm = turn.stages.find((s) => s.role === "llm");
  const firstTokenAt =
    llm?.startMs != null && llm.leadMs != null ? llm.startMs + llm.leadMs : null;
  const turnSelected = selection?.turn === turn.index && selection.operationId === null;

  return (
    <>
      <button
        type="button"
        onClick={() => onToggle(turn.index)}
        aria-expanded={open}
        className={`${styles.node} ${styles.turn} ${turnSelected ? styles.sel : ""}`}
      >
        <div className={styles.lab}>
          <Caret open={open} />
          <span className={styles.tnum}>T{String(turn.index).padStart(2, "0")}</span>
          {turn.interrupted ? (
            <span className={`${styles.chip} ${styles.barge}`}>barge-in</span>
          ) : null}
        </div>
        <div className={styles.gantt}>
          {turn.stages.map((stage) => (
            <Bar key={stage.operationId} stage={stage} scale={scale} />
          ))}
          {firstTokenAt != null ? (
            <div
              className={styles.mk}
              style={{ left: `${(firstTokenAt / scale) * 100}%` }}
              title={`first token ${formatMs(turn.firstToken.value)}`}
            />
          ) : null}
        </div>
        <div className={styles.dur}>
          {turn.totalMs == null ? "not observed" : `+${formatMs(turn.totalMs)}`}
        </div>
      </button>

      {open ? (
        <div className={styles.kids}>
          {turn.stages.map((stage) => {
            const stageSelected =
              selection?.turn === turn.index &&
              selection.operationId === stage.operationId;
            const color = roleColorVar(stage.role);
            return (
              <button
                key={stage.operationId}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onOperation(turn.index, stage.operationId);
                }}
                className={`${styles.node} ${styles.stage} ${stageSelected ? styles.sel : ""}`}
              >
                <div className={styles.lab}>
                  <span className={styles.sdot2} style={{ background: color }} />
                  <span className={styles.nm} style={{ color }}>
                    {stage.name}
                  </span>
                  <span className={styles.prov}>{operationSubtitle(stage)}</span>
                  {stage.statusView.abnormal ? (
                    <span
                      className={styles.statusChip}
                      style={{
                        color: toneColorVar(stage.statusView.tone),
                        borderColor: toneColorVar(stage.statusView.tone),
                      }}
                    >
                      {stage.statusView.label}
                    </span>
                  ) : null}
                </div>
                <div className={styles.gantt}>
                  <Bar stage={stage} scale={scale} />
                </div>
                <div className={styles.dur}>{stageReadout(stage)}</div>
              </button>
            );
          })}
        </div>
      ) : null}
    </>
  );
}

export function TurnTimeline({
  timeline,
  openTurns,
  selection,
  onToggleTurn,
  onSelectOperation,
}: {
  timeline: Timeline;
  openTurns: Set<number>;
  selection: Selection | null;
  onToggleTurn: (index: number) => void;
  onSelectOperation: (index: number, operationId: string) => void;
}) {
  const scale = timeline.scaleMs;
  const ticks = Array.from({ length: TICK_COUNT + 1 }, (_, i) =>
    Math.round((scale / TICK_COUNT) * i),
  );

  // The legend reflects the roles actually present, in a stable order.
  const ROLE_ORDER: OperationRole[] = [
    "stt",
    "llm",
    "tts",
    "agent",
    "tool",
    "transport",
    "render",
    "vad",
    "detection",
    "other",
  ];
  const present = new Set<OperationRole>();
  for (const turn of timeline.turns) {
    for (const stage of turn.stages) present.add(stage.role);
  }
  const legend = ROLE_ORDER.filter((role) => present.has(role));

  return (
    <section className={styles.wrap}>
      <div className={styles.panelHead}>
        <h2>Turn timeline</h2>
        <div className={styles.legend}>
          {legend.map((role) => (
            <span key={role}>
              <i className={styles.swatch} style={{ background: roleColorVar(role) }} />
              {role}
            </span>
          ))}
          <span>
            <i
              className={`${styles.swatch} ${styles.round}`}
              style={{ background: "var(--acc)" }}
            />
            first token
          </span>
        </div>
      </div>

      <div className={styles.axis}>
        <div />
        <div className={styles.ruler}>
          {ticks.map((tick) => (
            <span
              key={tick}
              className={styles.tick}
              style={{ left: `${(tick / scale) * 100}%` }}
            >
              {tick}
            </span>
          ))}
        </div>
      </div>

      <div className={styles.rows}>
        {timeline.turns.map((turn) => (
          <TurnRow
            key={turn.turnId}
            turn={turn}
            scale={scale}
            open={openTurns.has(turn.index)}
            selection={selection}
            onToggle={onToggleTurn}
            onOperation={onSelectOperation}
          />
        ))}
      </div>
    </section>
  );
}
