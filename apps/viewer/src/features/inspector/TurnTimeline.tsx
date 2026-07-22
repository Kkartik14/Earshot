import { formatMs } from "../../lib/format";
import styles from "./TurnTimeline.module.css";
import type { StageBar, StageName, Timeline } from "./timeline";

const LEGEND: StageName[] = ["stt", "llm", "tts"];
const TICK_COUNT = 6;

export interface Selection {
  turn: number;
  stage: StageName | null;
}

function Bar({ stage, scale }: { stage: StageBar; scale: number }) {
  if (stage.startMs == null) {
    return <div className={styles.unplaced} title={`${stage.name} timing unavailable`} />;
  }
  if (stage.timing === "point" || stage.endMs == null) {
    return (
      <div
        className={`${styles.point} ${styles[stage.name]}`}
        style={{ left: `${(stage.startMs / scale) * 100}%` }}
        title={`${stage.name} point · ${stage.provider ?? "?"} · interval not observed`}
      />
    );
  }
  const width = stage.endMs - stage.startMs;
  const leadPct =
    width > 0 && stage.leadMs != null ? Math.min(100, (stage.leadMs / width) * 100) : 0;
  return (
    <div
      className={`${styles.bar} ${styles[stage.name]}`}
      style={{
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
  onStage,
}: {
  turn: Timeline["turns"][number];
  scale: number;
  open: boolean;
  selection: Selection | null;
  onToggle: (index: number) => void;
  onStage: (index: number, stage: StageName) => void;
}) {
  const slow = (turn.firstToken.value ?? 0) > 500;
  const llm = turn.stages.find((s) => s.name === "llm");
  const firstTokenAt =
    llm?.startMs != null && llm.leadMs != null ? llm.startMs + llm.leadMs : null;
  const turnSelected = selection?.turn === turn.index && selection.stage === null;

  return (
    <>
      <button
        type="button"
        onClick={() => onToggle(turn.index)}
        aria-expanded={open}
        className={`${styles.node} ${styles.turn} ${slow ? styles.slow : ""} ${
          turnSelected ? styles.sel : ""
        }`}
      >
        <div className={styles.lab}>
          <Caret open={open} />
          <span className={styles.tnum}>T{String(turn.index).padStart(2, "0")}</span>
          {turn.interrupted ? (
            <span className={`${styles.chip} ${styles.barge}`}>barge-in</span>
          ) : null}
          {slow ? (
            <span className={`${styles.chip} ${styles.slowChip}`}>slow</span>
          ) : null}
        </div>
        <div className={styles.gantt}>
          {turn.stages.map((stage) => (
            <Bar key={stage.name} stage={stage} scale={scale} />
          ))}
          {firstTokenAt != null ? (
            <div
              className={styles.mk}
              style={{ left: `${(firstTokenAt / scale) * 100}%` }}
              title={`first token ${formatMs(turn.firstToken.value)}`}
            />
          ) : null}
        </div>
        <div className={`${styles.dur} ${slow ? styles.durSlow : ""}`}>
          +{formatMs(turn.totalMs)}
        </div>
      </button>

      {open ? (
        <div className={styles.kids}>
          {turn.stages.map((stage) => {
            const stageSelected =
              selection?.turn === turn.index && selection.stage === stage.name;
            return (
              <button
                key={stage.name}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onStage(turn.index, stage.name);
                }}
                className={`${styles.node} ${styles.stage} ${stageSelected ? styles.sel : ""}`}
              >
                <div className={styles.lab}>
                  <span
                    className={styles.sdot2}
                    style={{ background: `var(--${stage.name})` }}
                  />
                  <span className={styles.nm} style={{ color: `var(--${stage.name})` }}>
                    {stage.name}
                  </span>
                  <span className={styles.prov}>
                    {stage.provider ?? "?"} · {stage.model ?? "?"}
                  </span>
                </div>
                <div className={styles.gantt}>
                  <Bar stage={stage} scale={scale} />
                </div>
                <div className={styles.dur}>
                  {stage.leadMs == null ? "not observed" : formatMs(stage.leadMs)}
                </div>
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
  onSelectStage,
}: {
  timeline: Timeline;
  openTurns: Set<number>;
  selection: Selection | null;
  onToggleTurn: (index: number) => void;
  onSelectStage: (index: number, stage: StageName) => void;
}) {
  const scale = timeline.scaleMs;
  const ticks = Array.from({ length: TICK_COUNT + 1 }, (_, i) =>
    Math.round((scale / TICK_COUNT) * i),
  );

  return (
    <section className={styles.wrap}>
      <div className={styles.panelHead}>
        <h2>Turn timeline</h2>
        <div className={styles.legend}>
          {LEGEND.map((name) => (
            <span key={name}>
              <i className={styles.swatch} style={{ background: `var(--${name})` }} />
              {name}
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
            onStage={onSelectStage}
          />
        ))}
      </div>
    </section>
  );
}
