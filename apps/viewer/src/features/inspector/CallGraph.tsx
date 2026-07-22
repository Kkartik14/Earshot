import { useId, type CSSProperties } from "react";
import styles from "./CallGraph.module.css";
import { roleColorVar, roleLabel, type StageDetail, type TurnDetail } from "./timeline";

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

function subtitle(op: StageDetail): string {
  if (op.provider != null || op.model != null) {
    return `${op.provider ?? "?"} · ${shortModel(op.model)}`;
  }
  return roleLabel(op.role);
}

/** Faithful timing readout: lead when known, else the observed interval, else
 * the bare shape. Never a fabricated duration. */
function readout(op: StageDetail): string {
  if (op.leadMs != null) return `${Math.round(op.leadMs)}ms`;
  if (op.timing === "interval" && op.startMs != null && op.endMs != null) {
    return `${Math.round(op.endMs - op.startMs)}ms`;
  }
  if (op.timing === "point") return "point";
  return "not observed";
}

/** Renders the actual operations present as a generic vertical flow. There is
 * no invented cascade, no hardcoded playout node, and no causal edges — the
 * only connector is an arrival-order hint (which order they were observed in,
 * not causality). Real causal edges are a later pass. */
export function CallGraph({
  detail,
  onPick,
}: {
  detail: TurnDetail;
  onPick: (operationId: string) => void;
}) {
  const descriptionId = useId();
  const slowTurn = (detail.firstTokenMs ?? 0) > 500;
  const ops = detail.stages;

  const H = 8 + Math.max(0, ops.length - 1) * PIT + NH + 8;
  const W = NW + X * 2;

  const orderNote =
    ops.length > 1
      ? " Nodes are shown in arrival order, which does not imply causation."
      : "";
  const description =
    ops.length === 0
      ? "No operations were observed for this turn."
      : `Operations in arrival order: ${ops.map((op) => op.name).join(", ")}.` +
        orderNote +
        (detail.interrupted ? " An interruption was accepted during this turn." : "");

  return (
    <div className={styles.graph}>
      <svg
        viewBox={`0 0 ${W} ${Math.max(H, NH + 16)}`}
        width={W}
        height={Math.max(H, NH + 16)}
        xmlns="http://www.w3.org/2000/svg"
        role="group"
        aria-label="Turn call graph; select an operation to inspect it"
        aria-describedby={descriptionId}
      >
        <desc id={descriptionId}>{description}</desc>
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
        </defs>

        {ops.map((_, i) => {
          const y = 8 + i * PIT;
          const next = ops[i + 1];
          if (next == null) return null;
          // Arrival-order hint only — not a causal edge.
          return (
            <path
              key={`e${i}`}
              className={styles.edge}
              aria-hidden="true"
              d={`M${CX} ${y + NH}L${CX} ${y + PIT}`}
              markerEnd="url(#cg-arrow)"
            />
          );
        })}

        {ops.map((op, i) => {
          const y = 8 + i * PIT;
          const color = roleColorVar(op.role);
          const slow = op.role === "llm" && slowTurn;
          const sub = subtitle(op);
          const lat = readout(op);
          const activate = () => onPick(op.operationId);
          return (
            <g
              key={op.operationId}
              className={`${styles.gn} ${slow ? styles.slow : ""}`}
              style={{ "--nc": color } as CSSProperties}
              onClick={activate}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  activate();
                }
              }}
              role="button"
              tabIndex={0}
              aria-label={`${op.name} operation, ${sub}, ${lat}. Open operation detail.`}
            >
              <rect className={styles.box} x={X} y={y} width={NW} height={NH} rx={9} />
              <rect
                x={X}
                y={y + 9}
                width={3.5}
                height={NH - 18}
                rx={2}
                style={{ fill: color }}
              />
              <text className={styles.nm} x={X + 16} y={y + 19}>
                {op.name}
              </text>
              <text
                className={`${styles.lat} ${slow ? styles.slowLat : ""}`}
                x={X + NW - 13}
                y={y + 19}
                textAnchor="end"
              >
                {lat}
              </text>
              <text className={styles.sub} x={X + 16} y={y + 34}>
                {sub}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
