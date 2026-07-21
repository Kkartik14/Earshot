import { formatMs } from "../../lib/format";
import styles from "./TurnTimeline.module.css";
import type { StageName, Timeline, TurnView } from "./timeline";

const LEGEND: StageName[] = ["stt", "llm", "tts"];
const TICK_COUNT = 6;

function TurnRow({
  turn,
  scale,
  selected,
  onSelect,
}: {
  turn: TurnView;
  scale: number;
  selected: boolean;
  onSelect: (index: number) => void;
}) {
  const slow = (turn.firstToken.value ?? 0) > 500;
  const llm = turn.stages.find((s) => s.name === "llm");
  const firstTokenAt = llm ? llm.startMs + llm.leadMs : null;

  return (
    <button
      type="button"
      onClick={() => onSelect(turn.index)}
      aria-pressed={selected}
      className={`${styles.row} ${slow ? styles.slow : ""} ${selected ? styles.selected : ""}`}
    >
      <div className={styles.label}>
        <span className={styles.tnum}>T{String(turn.index).padStart(2, "0")}</span>
        {turn.interrupted ? (
          <span className={`${styles.chip} ${styles.barge}`}>barge-in</span>
        ) : null}
        {slow ? <span className={`${styles.chip} ${styles.slowChip}`}>slow</span> : null}
      </div>

      <div className={styles.track}>
        {turn.stages.map((stage) => {
          const width = stage.endMs - stage.startMs;
          const leadPct = width > 0 ? Math.min(100, (stage.leadMs / width) * 100) : 100;
          return (
            <div
              key={stage.name}
              className={`${styles.bar} ${styles[stage.name]}`}
              style={{
                left: `${(stage.startMs / scale) * 100}%`,
                width: `${(width / scale) * 100}%`,
              }}
              title={`${stage.name} · ${stage.provider ?? "?"} · ${formatMs(stage.leadMs)}`}
            >
              <div className={styles.tail} />
              <div className={styles.lead} style={{ width: `${leadPct}%` }} />
            </div>
          );
        })}
        {firstTokenAt != null ? (
          <div
            className={styles.marker}
            style={{ left: `${(firstTokenAt / scale) * 100}%` }}
            title={`first token ${formatMs(turn.firstToken.value)}`}
          />
        ) : null}
      </div>

      <div className={`${styles.dur} ${slow ? styles.durSlow : ""}`}>
        {formatMs(turn.totalMs)}
      </div>
    </button>
  );
}

export function TurnTimeline({
  timeline,
  selectedIndex,
  onSelect,
}: {
  timeline: Timeline;
  selectedIndex: number | null;
  onSelect: (index: number) => void;
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
            selected={turn.index === selectedIndex}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}
