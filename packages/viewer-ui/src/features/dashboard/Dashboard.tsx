import { useId, useState, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { EmptyState } from "../../components/EmptyState";
import { formatSeconds } from "../calls/format";
import {
  useDashboard,
  type DashboardBody,
  type DashboardTiles,
  type GroupBy,
  type LatencyTile,
} from "./api";
import styles from "./Dashboard.module.css";
import {
  bucketLabel,
  costPerMinute,
  coverage,
  perHundred,
  percentileParts,
  RANGES,
  rangeQuery,
  share,
  type Bucket,
  type RangeKey,
} from "./format";

const GROUP_LABELS: Record<GroupBy, string> = { framework: "Framework", agent: "Agent" };
const UNKNOWN = "unknown";

/** Dashboard v1: the project's projected calls over a range, by their own start
 * time. Every number names the turns or calls it covers; an unknown is a dash. */
export function Dashboard() {
  const [range, setRange] = useState<RangeKey>("24h");
  const [bucket, setBucket] = useState<Bucket>("hour");
  const [groupBy, setGroupBy] = useState<GroupBy>("framework");
  // The range ends when it was chosen, so a re-render never moves it.
  const [nowMs, setNowMs] = useState(() => Date.now());
  const query = useDashboard({ ...rangeQuery(range, nowMs), bucket, groupBy });

  const chooseRange = (key: RangeKey) => {
    setRange(key);
    setBucket(RANGES.find((item) => item.key === key)?.bucket ?? "day");
    setNowMs(Date.now());
  };

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className={styles.title}>Dashboard</h1>
        <p className={styles.subtitle}>
          Projected calls by the time their evidence started. Every number says which
          turns or calls it covers.
        </p>
        <div className={styles.controls}>
          <Select
            label="Range"
            value={range}
            onChange={(value) => chooseRange(value as RangeKey)}
            options={RANGES.map((item) => [item.key, item.label])}
          />
          <Select
            label="Bucket"
            value={bucket}
            onChange={(value) => setBucket(value as Bucket)}
            options={[
              ["hour", "Hour"],
              ["day", "Day"],
            ]}
          />
          <Select
            label="Group by"
            value={groupBy}
            onChange={(value) => setGroupBy(value as GroupBy)}
            options={Object.entries(GROUP_LABELS)}
          />
        </div>
      </header>
      <div className={styles.body}>
        {query.isPending ? <EmptyState title="Loading the dashboard…" /> : null}
        {query.isError ? (
          <EmptyState
            title="The dashboard is unavailable"
            hint={
              query.error instanceof ApiError
                ? query.error.message
                : "The backend may be unavailable."
            }
          />
        ) : null}
        {query.data ? <DashboardView body={query.data} /> : null}
      </div>
    </div>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<readonly [string, string]>;
}) {
  const id = useId();
  return (
    <div className={styles.control}>
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
      </select>
    </div>
  );
}

/** One response's tiles, chart and tables. */
export function DashboardView({ body }: { body: DashboardBody }) {
  const { overall } = body;
  if (overall.calls === 0) {
    return (
      <EmptyState
        title="No projected calls in this range"
        hint={
          body.not_projected_calls > 0
            ? `${body.not_projected_calls} received in this range ${body.not_projected_calls === 1 ? "is" : "are"} not projected yet.`
            : "Calls appear here once they close and the server projects them."
        }
      />
    );
  }
  return (
    <>
      <div className={styles.tiles}>
        <Tile title="Calls" value={String(overall.calls)}>
          {body.not_projected_calls > 0
            ? `${body.not_projected_calls} more received, not projected yet`
            : "none waiting for projection"}
        </Tile>
        <LatencyTileView title="Reported latency" tile={overall.reported_latency}>
          as the framework reports each turn
        </LatencyTileView>
        <LatencyTileView title="Audible gap" tile={overall.audible_gap}>
          user stops to agent starts, measured on the recording
        </LatencyTileView>
        <Tile
          title="Interruption rate"
          value={perHundred(
            overall.interruption_rate.barge_ins,
            overall.interruption_rate.turns,
          )}
        >
          barge-ins ·{" "}
          {coverage(
            overall.interruption_rate.calls,
            overall.interruption_rate.of_calls,
            "calls",
          )}{" "}
          with a recording on one timeline
        </Tile>
        <Tile
          title="Dead air"
          value={
            overall.dead_air.calls === 0
              ? "—"
              : `${share(overall.dead_air.dead_air_ms, overall.dead_air.duration_ms)} of call time`
          }
        >
          {coverage(overall.dead_air.calls, overall.dead_air.of_calls, "calls")} with a
          recording on one timeline
        </Tile>
        <CostTile tiles={overall} />
      </div>
      <BucketChart body={body} />
      <GroupTable body={body} />
      <LlmTable tiles={overall} />
    </>
  );
}

function Tile({
  title,
  value,
  children,
}: {
  title: string;
  value: ReactNode;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className={styles.tile} aria-labelledby={id}>
      <h2 id={id} className={styles.tileLabel}>
        {title}
      </h2>
      <p className={styles.tileValue}>{value}</p>
      <p className={styles.tileNote}>{children}</p>
    </section>
  );
}

function LatencyTileView({
  title,
  tile,
  children,
}: {
  title: string;
  tile: LatencyTile;
  children: ReactNode;
}) {
  const { p50, p95 } = percentileParts(tile);
  return (
    <Tile
      title={title}
      value={
        <>
          <span>p50 {p50}</span> <span className={styles.p95}>p95 {p95}</span>
        </>
      }
    >
      {coverage(tile.turns, tile.of_turns, "turns")} ·{" "}
      {coverage(tile.calls, tile.of_calls, "calls")}
      <span className={styles.tileHint}>{children}</span>
    </Tile>
  );
}

function CostTile({ tiles }: { tiles: DashboardTiles }) {
  const id = useId();
  const { earshot, provider } = tiles.cost_per_minute;
  return (
    <section className={`${styles.tile} ${styles.costTile}`} aria-labelledby={id}>
      <h2 id={id} className={styles.tileLabel}>
        Cost per minute
      </h2>
      <div className={styles.costs}>
        <CostRate
          title="Earshot estimate"
          value={costPerMinute(earshot.nano_usd_per_minute)}
        >
          {coverage(earshot.calls, earshot.of_calls, "calls")} with a complete cost
        </CostRate>
        <CostRate
          title="Provider reported"
          value={costPerMinute(provider.nano_usd_per_minute)}
        >
          {coverage(provider.calls, provider.of_calls, "calls")}
          {provider.bases.length > 0 ? ` · ${provider.bases.join(", ")}` : ""}
        </CostRate>
      </div>
      <p className={styles.tileNote}>
        Side by side, never added: a provider's cost already includes the stages Earshot
        prices.
      </p>
    </section>
  );
}

function CostRate({
  title,
  value,
  children,
}: {
  title: string;
  value: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div role="group" aria-labelledby={id} className={styles.costRate}>
      <span id={id} className={styles.costLabel}>
        {title}
      </span>
      <span className={styles.tileValue}>{value}</span>
      <span className={styles.tileNote}>{children}</span>
    </div>
  );
}

function BucketChart({ body }: { body: DashboardBody }) {
  const [asTable, setAsTable] = useState(false);
  const tableId = useId();
  const unit = body.bucket;
  const counts = body.buckets.map((item) => item.tiles.calls);
  const peak = Math.max(1, ...counts);
  const title = `Calls per ${unit}`;
  const plural = `${unit}${body.buckets.length === 1 ? "" : "s"}`;
  return (
    <section className={styles.section} aria-labelledby={`${tableId}-title`}>
      <div className={styles.sectionHead}>
        <h2 id={`${tableId}-title`} className={styles.sectionTitle}>
          {title} <span className={styles.muted}>(UTC)</span>
        </h2>
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={asTable}
          aria-controls={tableId}
          onClick={() => setAsTable((value) => !value)}
        >
          {asTable ? "Hide table" : "Show as table"}
        </button>
      </div>
      <div
        role="img"
        aria-label={`${title}: ${overallCalls(counts)} calls in ${body.buckets.length} ${plural}`}
        className={styles.bars}
        // A gap between bars while they are wide enough to keep one.
        style={{ gap: body.buckets.length > 120 ? 0 : 2 }}
      >
        {body.buckets.map((item) => (
          <span
            key={item.start_unix_nano}
            className={styles.barSlot}
            title={`${bucketLabel(item.start_unix_nano, unit)}: ${item.tiles.calls} calls`}
          >
            <span
              className={styles.bar}
              style={{ height: `${(item.tiles.calls / peak) * 100}%` }}
            />
          </span>
        ))}
      </div>
      {body.buckets.length > 0 ? (
        <div className={styles.axis} aria-hidden="true">
          <span>{bucketLabel(body.buckets[0].start_unix_nano, unit)}</span>
          <span>
            {bucketLabel(body.buckets[body.buckets.length - 1].start_unix_nano, unit)}
          </span>
        </div>
      ) : null}
      {asTable ? (
        <div id={tableId} className={styles.tableWrap}>
          <table className={styles.table}>
            <caption className={styles.caption}>{`${title} (UTC)`}</caption>
            <thead>
              <tr>
                <th scope="col">{unit === "hour" ? "Hour" : "Day"}</th>
                <th scope="col" className={styles.num}>
                  Calls
                </th>
                <th scope="col" className={styles.num}>
                  Reported p95
                </th>
                <th scope="col" className={styles.num}>
                  Audible gap p95
                </th>
              </tr>
            </thead>
            <tbody>
              {body.buckets.map((item) => (
                <tr key={item.start_unix_nano}>
                  <th scope="row">{bucketLabel(item.start_unix_nano, unit)}</th>
                  <td className={styles.num}>{item.tiles.calls}</td>
                  <td className={styles.num}>
                    {formatSeconds(item.tiles.reported_latency.p95_ms)}
                  </td>
                  <td className={styles.num}>
                    {formatSeconds(item.tiles.audible_gap.p95_ms)}
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

const overallCalls = (counts: number[]) => counts.reduce((sum, value) => sum + value, 0);

function GroupTable({ body }: { body: DashboardBody }) {
  const label = GROUP_LABELS[body.group_by];
  return (
    <section className={styles.section}>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <caption className={styles.caption}>{`By ${label.toLowerCase()}`}</caption>
          <thead>
            <tr>
              <th scope="col">{label}</th>
              <th scope="col" className={styles.num}>
                Calls
              </th>
              <th scope="col" className={styles.num}>
                Reported p50 / p95
              </th>
              <th scope="col" className={styles.num}>
                Audible gap p95
              </th>
              <th scope="col" className={styles.num}>
                Interruptions
              </th>
              <th scope="col" className={styles.num}>
                Dead air
              </th>
              <th scope="col" className={styles.num}>
                Earshot cost
              </th>
              <th scope="col" className={styles.num}>
                Provider cost
              </th>
            </tr>
          </thead>
          <tbody>
            {body.groups.map(({ value, tiles }) => (
              <tr key={value ?? "\u0000unknown"}>
                <th scope="row" className={styles.group}>
                  {value ?? UNKNOWN}
                </th>
                <td className={styles.num}>{tiles.calls}</td>
                <td className={styles.num}>
                  {formatSeconds(tiles.reported_latency.p50_ms)} /{" "}
                  {formatSeconds(tiles.reported_latency.p95_ms)}
                </td>
                <td className={styles.num}>{formatSeconds(tiles.audible_gap.p95_ms)}</td>
                <td className={styles.num}>
                  {perHundred(
                    tiles.interruption_rate.barge_ins,
                    tiles.interruption_rate.turns,
                  )}
                </td>
                <td className={styles.num}>
                  {share(tiles.dead_air.dead_air_ms, tiles.dead_air.duration_ms)}
                </td>
                <td className={styles.num}>
                  {costPerMinute(tiles.cost_per_minute.earshot.nano_usd_per_minute)}
                </td>
                <td className={styles.num}>
                  {costPerMinute(tiles.cost_per_minute.provider.nano_usd_per_minute)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LlmTable({ tiles }: { tiles: DashboardTiles }) {
  const llm = tiles.llm_first_token;
  const title = `LLM first token by provider (${coverage(llm.calls, llm.of_calls, "calls")})`;
  if (llm.providers.length === 0) {
    return (
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        <p className={styles.note}>No call in this range reports an LLM first token.</p>
      </section>
    );
  }
  return (
    <section className={styles.section}>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <caption className={styles.caption}>{title}</caption>
          <thead>
            <tr>
              <th scope="col">Provider</th>
              <th scope="col">Basis</th>
              <th scope="col" className={styles.num}>
                p50
              </th>
              <th scope="col" className={styles.num}>
                p95
              </th>
              <th scope="col" className={styles.num}>
                Turns
              </th>
              <th scope="col" className={styles.num}>
                Calls
              </th>
            </tr>
          </thead>
          <tbody>
            {llm.providers.map((item) => (
              <tr key={`${item.provider ?? ""}\u0000${item.basis}`}>
                <th scope="row" className={styles.group}>
                  {item.provider ?? UNKNOWN}
                </th>
                <td>{item.basis.replace(/_/g, " ")}</td>
                <td className={styles.num}>{formatSeconds(item.p50_ms)}</td>
                <td className={styles.num}>{formatSeconds(item.p95_ms)}</td>
                <td className={styles.num}>{item.turns}</td>
                <td className={styles.num}>{item.calls}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className={styles.note}>
        Values on different bases do not compare: "first token" runs from the end of the
        user's turn, "provider stage direct" is the provider's own timing of the LLM.
      </p>
    </section>
  );
}
