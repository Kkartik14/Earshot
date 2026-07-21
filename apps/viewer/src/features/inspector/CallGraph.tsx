import type { CSSProperties } from "react";
import styles from "./CallGraph.module.css";
import type { StageName, TurnDetail } from "./timeline";

const REL: Record<StageName, string> = {
  stt: "transcribes",
  llm: "produces",
  tts: "emits",
};
const shortModel = (m?: string) =>
  m == null
    ? "?"
    : m.includes("whisper")
      ? "whisper"
      : m.includes("llama")
        ? "llama-3.1"
        : m;

const X = 12;
const NW = 214;
const NH = 42;
const PIT = 62;
const CX = X + NW / 2;

type Row =
  | { term: false; name: StageName; lat: string; sub: string; slow: boolean; nc: string }
  | { term: true; name: string; lat: string; sub: string };

export function CallGraph({
  detail,
  onPick,
}: {
  detail: TurnDetail;
  onPick: (stage: StageName) => void;
}) {
  const slowTurn = (detail.firstTokenMs ?? 0) > 500;
  const rows: Row[] = [
    ...detail.stages.map((s) => ({
      term: false as const,
      name: s.name,
      lat: `${Math.round(s.leadMs)}ms`,
      sub: `${s.provider ?? "?"} · ${shortModel(s.model)}`,
      slow: s.name === "llm" && slowTurn,
      nc: `var(--${s.name})`,
    })),
    { term: true as const, name: "playout", lat: "not observed", sub: "client render" },
  ];

  const H = 8 + (rows.length - 1) * PIT + NH + 8;
  const W = detail.interrupted ? 348 : NW + X * 2;

  return (
    <div className={styles.graph}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <marker
            id="cg-arrow"
            markerWidth="7"
            markerHeight="7"
            refX="6"
            refY="3.5"
            orient="auto"
          >
            <path d="M0 0L7 3.5L0 7Z" fill="var(--tx-low)" />
          </marker>
          <marker
            id="cg-arrow-t"
            markerWidth="7"
            markerHeight="7"
            refX="6"
            refY="3.5"
            orient="auto"
          >
            <path d="M0 0L7 3.5L0 7Z" fill="var(--tts)" />
          </marker>
        </defs>

        {rows.map((row, i) => {
          const y = 8 + i * PIT;
          const next = rows[i + 1];
          const edge =
            next != null && !row.term ? (
              <g key={`e${i}`}>
                <path
                  className={styles.edge}
                  d={`M${CX} ${y + NH}L${CX} ${y + PIT}`}
                  markerEnd="url(#cg-arrow)"
                />
                <text
                  className={styles.elab}
                  x={CX + 13}
                  y={(y + NH + (y + PIT)) / 2 + 3.5}
                >
                  {REL[row.name]}
                </text>
              </g>
            ) : null;
          return edge;
        })}

        {rows.map((row, i) => {
          const y = 8 + i * PIT;
          if (row.term) {
            return (
              <g key={`n${i}`} className={`${styles.gn} ${styles.term}`}>
                <rect className={styles.box} x={X} y={y} width={NW} height={NH} rx={9} />
                <text className={styles.nm} x={X + 16} y={y + 19}>
                  {row.name}
                </text>
                <text
                  className={styles.termLat}
                  x={X + NW - 13}
                  y={y + 19}
                  textAnchor="end"
                >
                  {row.lat}
                </text>
                <text className={styles.sub} x={X + 16} y={y + 34}>
                  {row.sub}
                </text>
              </g>
            );
          }
          return (
            <g
              key={`n${i}`}
              className={`${styles.gn} ${row.slow ? styles.slow : ""}`}
              style={{ "--nc": row.nc } as CSSProperties}
              onClick={() => onPick(row.name)}
              role="button"
              tabIndex={0}
            >
              <rect className={styles.box} x={X} y={y} width={NW} height={NH} rx={9} />
              <rect x={X} y={y + 9} width={3.5} height={NH - 18} rx={2} fill={row.nc} />
              <text className={styles.nm} x={X + 16} y={y + 19}>
                {row.name}
              </text>
              <text
                className={`${styles.lat} ${row.slow ? styles.slowLat : ""}`}
                x={X + NW - 13}
                y={y + 19}
                textAnchor="end"
              >
                {row.lat}
              </text>
              <text className={styles.sub} x={X + 16} y={y + 34}>
                {row.sub}
              </text>
            </g>
          );
        })}

        {detail.interrupted
          ? (() => {
              const ttsY = 8 + 2 * PIT;
              const ty = ttsY + NH / 2;
              const bx = X + NW + 18;
              const bw = 98;
              const by = ty - 16;
              return (
                <g>
                  <path
                    className={`${styles.edge} ${styles.intr}`}
                    d={`M${bx} ${ty}L${X + NW} ${ty}`}
                    markerEnd="url(#cg-arrow-t)"
                  />
                  <text
                    className={styles.ilab}
                    x={bx + bw / 2}
                    y={by - 6}
                    textAnchor="middle"
                  >
                    interrupts
                  </text>
                  <rect
                    className={styles.bargeBox}
                    x={bx}
                    y={by}
                    width={bw}
                    height={32}
                    rx={8}
                  />
                  <text
                    className={styles.bargeTx}
                    x={bx + bw / 2}
                    y={by + 20}
                    textAnchor="middle"
                  >
                    ⚡ barge-in
                  </text>
                </g>
              );
            })()
          : null}
      </svg>
    </div>
  );
}
