import { formatSeconds, formatUsd } from "../calls/format";

const EM_DASH = "—";
const MS_PER_HOUR = 3_600_000;
const MS_PER_DAY = 24 * MS_PER_HOUR;

export const RANGES = [
  { key: "24h", label: "Last 24 hours", ms: MS_PER_DAY, bucket: "hour" },
  { key: "7d", label: "Last 7 days", ms: 7 * MS_PER_DAY, bucket: "day" },
  { key: "30d", label: "Last 30 days", ms: 30 * MS_PER_DAY, bucket: "day" },
] as const;
export type RangeKey = (typeof RANGES)[number]["key"];
export type Bucket = "hour" | "day";

/** The `from` and `to` of the range that ends at `nowMs`, as the API takes them:
 * Unix nanoseconds as decimal strings (exact, unlike a JS number). */
export function rangeQuery(range: RangeKey, nowMs: number): { from: string; to: string } {
  const span = RANGES.find((item) => item.key === range)?.ms ?? MS_PER_DAY;
  const end = Math.floor(nowMs);
  return { from: `${end - span}000000`, to: `${end}000000` };
}

/** "n of N calls", or "no calls" when there is nothing to cover. */
export function coverage(covered: number, of: number, unit: "calls" | "turns"): string {
  const noun = (count: number) => (count === 1 ? unit.slice(0, -1) : unit);
  return of === 0 ? `no ${unit}` : `${covered} of ${of} ${noun(of)}`;
}

/** Barge-ins per 100 turns; a dash when nothing measured them. */
export function perHundred(count: number | null, turns: number): string {
  if (count == null || turns === 0) return EM_DASH;
  const rate = (count * 100) / turns;
  return `${Number.isInteger(rate) ? rate : rate.toFixed(1)} per 100 turns`;
}

/** `part` as a percentage of `whole`; a dash when either is unknown. */
export function share(part: number | null, whole: number | null): string {
  if (part == null || whole == null || whole === 0) return EM_DASH;
  const percent = (part * 100) / whole;
  return `${Number.isInteger(percent) ? percent : percent.toFixed(1)}%`;
}

/** A nano-USD-per-minute rate; a dash when no call reports one. */
export function costPerMinute(nanoUsd: number | null): string {
  return nanoUsd == null ? EM_DASH : `${formatUsd(nanoUsd)}/min`;
}

/** "p50 x · p95 y" parts of a latency tile, in seconds; dashes when unknown. */
export function percentileParts(tile: { p50_ms: number | null; p95_ms: number | null }) {
  return { p50: formatSeconds(tile.p50_ms), p95: formatSeconds(tile.p95_ms) };
}

const HOUR_LABEL = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const DAY_LABEL = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  month: "short",
  day: "numeric",
  year: "numeric",
});

/** A bucket's start in UTC, where the server cuts its buckets. */
export function bucketLabel(startUnixNano: string, bucket: Bucket): string {
  const ms = Number(BigInt(startUnixNano) / 1_000_000n);
  return (bucket === "hour" ? HOUR_LABEL : DAY_LABEL).format(ms);
}
