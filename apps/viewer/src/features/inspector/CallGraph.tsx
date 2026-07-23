import { useId, type CSSProperties } from "react";
import { toneColorVar } from "../../lib/status";
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
const NH = 46;
const PIT = 66;
const TOP = 10;

/** A verb phrase for a causal relationship, used in the accessible description
 * and as the edge label. Unknown relationships fall back to the raw token. */
function relationshipVerb(relationship: string): string {
  switch (relationship) {
    case "retries":
      return "retries";
    case "supersedes":
      return "supersedes";
    case "consumes":
      return "consumes";
    case "produced_by":
      return "produced by";
    case "handoff":
      return "hands off to";
    case "duplicates":
      return "duplicates";
    case "interrupts":
      return "interrupts";
    case "parent":
      return "parents";
    default:
      return relationship.replace(/_/g, " ");
  }
}

function subtitle(op: StageDetail): string {
  if (op.provider != null || op.model != null) {
    return `${op.provider ?? "?"} · ${shortModel(op.model)}`;
  }
  return roleLabel(op.role);
}

/** Faithful timing readout: lead when known, else the observed interval, else
 * the bare shape. Never a fabricated duration. A start uncertainty, when the
 * analyzer supplied one, is shown as a ± annotation rather than dropped. */
function readout(op: StageDetail): string {
  let base: string;
  if (op.leadMs != null) base = `${Math.round(op.leadMs)}ms`;
  else if (op.timing === "interval" && op.startMs != null && op.endMs != null)
    base = `${Math.round(op.endMs - op.startMs)}ms`;
  else if (op.timing === "point") base = "point";
  else base = "not observed";
  return op.startUncertaintyMs != null
    ? `${base} ±${Math.round(op.startUncertaintyMs)}ms`
    : base;
}

/** Renders the operations present as a vertical flow, overlaid with source-authored
 * parent/link edges. There is no invented cascade or arrival-order connector: an
 * operation with no observed graph relationship simply has no drawn edge. */
export function CallGraph({
  detail,
  onPick,
}: {
  detail: TurnDetail;
  onPick: (operationId: string) => void;
}) {
  const descriptionId = useId();
  const arrowId = useId().replace(/:/g, "");
  const ops = detail.stages;
  const indexById = new Map(ops.map((op, i) => [op.operationId, i]));

  // Only edges whose both endpoints are nodes in this turn are drawable.
  const edges = detail.edges.filter(
    (e) => indexById.has(e.fromOperationId) && indexById.has(e.toOperationId),
  );
  const hasEdges = edges.length > 0;
  const gutter = hasEdges ? 66 : 14;

  const nodeY = (i: number) => TOP + i * PIT;
  const rightEdge = X + NW;

  const H = TOP * 2 + Math.max(0, ops.length - 1) * PIT + NH;
  const W = X + NW + gutter;

  const edgeSentences = edges.map((e) => {
    const from = ops[indexById.get(e.fromOperationId) as number];
    const to = ops[indexById.get(e.toOperationId) as number];
    return `${from.name} ${relationshipVerb(e.relationship)} ${to.name}`;
  });
  const edgeLabel = edges.some((edge) => edge.relationship === "parent")
    ? "Graph relationships"
    : "Causal links";
  const description =
    ops.length === 0
      ? "No operations were observed for this turn."
      : (hasEdges
          ? `${edgeLabel}: ${edgeSentences.join("; ")}.`
          : "No causal links were recorded between these operations.") +
        (detail.interrupted ? " An interruption was accepted during this turn." : "");

  return (
    <div className={styles.graph}>
      <svg
        viewBox={`0 0 ${W} ${Math.max(H, NH + TOP * 2)}`}
        width={W}
        height={Math.max(H, NH + TOP * 2)}
        xmlns="http://www.w3.org/2000/svg"
        role="group"
        aria-label="Turn call graph; select an operation to inspect it"
        aria-describedby={descriptionId}
      >
        <desc id={descriptionId}>{description}</desc>
        <defs>
          <marker
            id={arrowId}
            markerWidth="7"
            markerHeight="7"
            refX="6"
            refY="3.5"
            orient="auto"
          >
            <path d="M0 0L7 3.5L0 7Z" fill="var(--tx-low)" />
          </marker>
        </defs>

        {/* Real causal edges, routed in the right gutter. Each is an arc from
            the linking operation to the operation it references. */}
        {edges.map((e, k) => {
          const fromI = indexById.get(e.fromOperationId) as number;
          const toI = indexById.get(e.toOperationId) as number;
          const y0 = nodeY(fromI) + NH / 2;
          const y1 = nodeY(toI) + NH / 2;
          const bulge = 20 + (k % 3) * 14;
          const cx = rightEdge + bulge;
          const midY = (y0 + y1) / 2;
          return (
            <g key={`edge-${k}`} aria-hidden="true">
              <path
                className={styles.edge}
                d={`M${rightEdge} ${y0} Q${cx} ${midY} ${rightEdge} ${y1}`}
                markerEnd={`url(#${arrowId})`}
              />
              <text className={styles.edgeLabel} x={cx} y={midY} dy="0.32em">
                {e.relationship}
              </text>
            </g>
          );
        })}

        {ops.map((op, i) => {
          const y = nodeY(i);
          const color = roleColorVar(op.role);
          const sub = subtitle(op);
          const lat = readout(op);
          const badge = op.statusView.abnormal ? op.statusView.label : null;
          const badgeColor = toneColorVar(op.statusView.tone);
          const externalLinks = op.links.filter((l) => !l.resolved);
          const activate = () => onPick(op.operationId);
          const ariaBits = [`${op.name} operation`, sub, lat];
          if (badge) ariaBits.push(`status ${badge}`);
          if (op.interruptedByEvent) ariaBits.push("interrupted");
          for (const l of externalLinks)
            ariaBits.push(`${relationshipVerb(l.relationship)} ${l.targetScope}`);
          return (
            <g
              key={op.operationId}
              className={styles.gn}
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
              aria-label={`${ariaBits.join(", ")}. Open operation detail.`}
            >
              <rect className={styles.box} x={X} y={y} width={NW} height={NH} rx={9} />
              <rect
                x={X}
                y={y + 10}
                width={3.5}
                height={NH - 20}
                rx={2}
                style={{ fill: color }}
              />
              <text className={styles.nm} x={X + 16} y={y + 19}>
                {op.name}
              </text>
              <text className={styles.lat} x={X + NW - 13} y={y + 19} textAnchor="end">
                {lat}
              </text>
              <text className={styles.sub} x={X + 16} y={y + 35}>
                {sub}
                {op.interruptedByEvent ? "  · interrupted" : ""}
                {externalLinks.length > 0
                  ? `  · ↗ ${externalLinks.map((l) => l.relationship).join(", ")}`
                  : ""}
              </text>
              {badge ? (
                <g style={{ "--bc": badgeColor } as CSSProperties}>
                  <rect
                    className={styles.badge}
                    x={X + NW - 13 - (badge.length * 6.1 + 12)}
                    y={y + NH - 17}
                    width={badge.length * 6.1 + 12}
                    height={14}
                    rx={4}
                  />
                  <text
                    className={styles.badgeText}
                    x={X + NW - 13 - (badge.length * 6.1 + 12) / 2}
                    y={y + NH - 7}
                    textAnchor="middle"
                  >
                    {badge}
                  </text>
                </g>
              ) : null}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
