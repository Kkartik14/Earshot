import { useState } from "react";
import { useTurnMetrics } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { formatMs } from "../../lib/format";
import styles from "./FleetDashboard.module.css";
import {
  GROUP_BYS,
  METRICS,
  budgetFor,
  rankByP95,
  summarizeGroups,
  type GroupBy,
  type MetricKey,
} from "./fleet";

function Tile({ label, value, flag }: { label: string; value: string; flag?: boolean }) {
  return (
    <div className={`${styles.tile} ${flag ? styles.flag : ""}`}>
      <span className={styles.tileLabel}>{label}</span>
      <span className={styles.tileValue}>{value}</span>
    </div>
  );
}

export function FleetDashboard() {
  const [metric, setMetric] = useState<MetricKey>("first_token_ms");
  const [groupBy, setGroupBy] = useState<GroupBy>("model");
  const query = useTurnMetrics(metric, groupBy);

  const groups = query.data?.groups ?? [];
  const ranked = rankByP95(groups);
  const summary = summarizeGroups(groups);
  const budget = budgetFor(metric);
  const maxP95 = summary.worstP95 ?? 0;
  const flag = (v: number | null | undefined) =>
    budget != null && v != null && v > budget;
  const groupLabel = GROUP_BYS.find((g) => g.key === groupBy)?.label ?? "Group";
  const metricLabel = METRICS.find((m) => m.key === metric)?.label ?? "Metric";

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <div>
            <h1 className={styles.title}>Fleet metrics</h1>
            <p className={styles.subtitle}>
              {metricLabel} latency across every stored session, by{" "}
              {groupLabel.toLowerCase()}.
            </p>
          </div>
          <div className={styles.controls}>
            <label className={styles.control}>
              <span>Metric</span>
              <select
                value={metric}
                onChange={(e) => setMetric(e.target.value as MetricKey)}
              >
                {METRICS.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.control}>
              <span>Group by</span>
              <select
                value={groupBy}
                onChange={(e) => setGroupBy(e.target.value as GroupBy)}
              >
                {GROUP_BYS.map((g) => (
                  <option key={g.key} value={g.key}>
                    {g.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>

        <div className={styles.tiles}>
          <Tile label="Turns" value={String(summary.turns)} />
          <Tile
            label="Measured"
            value={
              summary.coveragePct == null ? "—" : `${Math.round(summary.coveragePct)}%`
            }
          />
          <Tile
            label="Slowest p95"
            value={formatMs(summary.worstP95)}
            flag={flag(summary.worstP95)}
          />
          <Tile label="Fastest p50" value={formatMs(summary.bestP50)} />
        </div>
      </header>

      <div className={styles.body}>
        {query.isPending ? <EmptyState title="Loading fleet metrics…" /> : null}
        {query.isError ? (
          <EmptyState
            title="Couldn't load fleet metrics"
            hint="The backend may be unavailable."
          />
        ) : null}
        {query.isSuccess && groups.length === 0 ? (
          <EmptyState
            title="No turns yet"
            hint="Ingest a voice session to populate fleet metrics."
          />
        ) : null}

        {query.isSuccess && groups.length > 0 ? (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>{groupLabel}</th>
                  <th className={styles.num}>Turns</th>
                  <th className={styles.num}>Avail</th>
                  <th className={styles.num}>Avg</th>
                  <th className={styles.num}>p50</th>
                  <th className={styles.p95col}>p95</th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((g, i) => {
                  const pct =
                    maxP95 > 0 && g.p95_ms != null ? (g.p95_ms / maxP95) * 100 : 0;
                  const availPct =
                    g.turn_count > 0
                      ? Math.round((g.available_count / g.turn_count) * 100)
                      : 0;
                  return (
                    <tr key={`${g.group}-${i}`}>
                      <td className={styles.group}>{g.group || "—"}</td>
                      <td className={styles.num}>{g.turn_count}</td>
                      <td className={styles.num}>{availPct}%</td>
                      <td className={styles.num}>{formatMs(g.average_ms)}</td>
                      <td className={styles.num}>{formatMs(g.p50_ms)}</td>
                      <td className={styles.p95col}>
                        <div className={styles.p95cell}>
                          <div className={styles.barTrack}>
                            <div
                              className={`${styles.barFill} ${
                                flag(g.p95_ms) ? styles.barCrit : ""
                              }`}
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                          <span
                            className={`${styles.p95val} ${flag(g.p95_ms) ? styles.crit : ""}`}
                          >
                            {formatMs(g.p95_ms)}
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}

        {budget != null && groups.length > 0 ? (
          <p className={styles.note}>
            Budget for {metricLabel.toLowerCase()}: {budget}ms — groups over budget are
            flagged. Percentiles are per group; a fleet-wide percentile can't be averaged
            from them.
          </p>
        ) : null}
      </div>
    </div>
  );
}
