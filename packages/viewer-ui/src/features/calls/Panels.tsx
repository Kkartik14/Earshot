import { useId } from "react";
import type { CallCost, CallSignals, CostItem } from "./api";
import { costReadout, formatSeconds, formatUsd, humanize } from "./format";
import styles from "./CallView.module.css";

const count = (value: number | null) => (value == null ? "—" : String(value));

/** Conversation signals from the recordings' speech runs. Unknown is a dash, and
 * cross-channel signals are unknown unless both channels share one timeline. */
export function SignalsPanel({ signals }: { signals: CallSignals }) {
  const headingId = useId();
  const rows: [string, string][] = [
    ["User talk time", formatSeconds(signals.talk_time_user_ms)],
    ["Agent talk time", formatSeconds(signals.talk_time_agent_ms)],
    ["Overlap", formatSeconds(signals.overlap_ms)],
    ["Barge-ins", count(signals.barge_in_count)],
    ["Talk-overs", count(signals.talk_over_count)],
    ["Long silences", count(signals.long_silence_count)],
    ["Dead air", formatSeconds(signals.dead_air_ms)],
  ];
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.panelTitle}>
        Signals
      </h2>
      {signals.one_timeline == null ? (
        <p className={styles.note}>No analysed recording, so there are no signals.</p>
      ) : null}
      {signals.one_timeline === false ? (
        <p className={styles.note}>
          The user&apos;s and the agent&apos;s recordings are not on one timeline, so
          overlap, barge-ins, talk-overs, silences and dead air are not computed.
        </p>
      ) : null}
      <dl className={styles.facts}>
        {rows.map(([label, value]) => (
          <div key={label} className={styles.fact}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function usage(item: CostItem): string {
  if (item.quantity == null) return "—";
  return item.unit == null
    ? String(item.quantity)
    : `${item.quantity} ${humanize(item.unit)}`;
}

function price(item: CostItem): string {
  if (item.price_nano_usd == null || item.per_quantity == null) return "—";
  const per = item.per_quantity === 1 ? "" : `${item.per_quantity} `;
  return `${formatUsd(item.price_nano_usd)} per ${per}${humanize(item.unit ?? "unit")}`;
}

/** The call's cost: the priced lower bound, the unknown items, and the estimate
 * kept apart from it; then every item with its status. */
export function CostPanel({ cost, items }: { cost: CallCost; items: CostItem[] }) {
  const headingId = useId();
  const readout = costReadout(cost);
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.panelTitle}>
        Cost
      </h2>
      <p className={styles.costTotal}>
        <span className={styles.costAmount}>{readout.amount}</span>
        {readout.estimate ? (
          <span className={styles.costEstimate}>{readout.estimate}</span>
        ) : null}
      </p>
      {cost.lower_bound_nano_usd == null ? (
        <p className={styles.note}>
          No cost items: the call reported no usage Earshot could meter.
        </p>
      ) : null}
      {items.length > 0 ? (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <caption className={styles.caption}>
              Priced items make up the amount; estimated items are only estimated; items
              with no price or no reported usage are counted as unknown.
            </caption>
            <thead>
              <tr>
                <th scope="col">Operation</th>
                <th scope="col">Provider · model</th>
                <th scope="col">Usage</th>
                <th scope="col">Price</th>
                <th scope="col" className={styles.num}>
                  Cost
                </th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={index}>
                  <td>{item.operation}</td>
                  <td className={styles.mono}>
                    {item.provider ?? "unknown"} · {item.model ?? "unknown"}
                  </td>
                  <td>{usage(item)}</td>
                  <td>{price(item)}</td>
                  <td className={styles.num}>
                    {item.cost_nano_usd == null ? "—" : formatUsd(item.cost_nano_usd)}
                  </td>
                  <td>
                    <span className={styles.status} data-status={item.status}>
                      {humanize(item.status)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
