const EM_DASH = "—";

/** A latency in milliseconds, or an em-dash when absent. */
export function formatMs(value: number | null | undefined): string {
  return value == null ? EM_DASH : `${Math.round(value)}ms`;
}

/** A duration given in milliseconds, rendered compactly (ms under 1s, else s). */
export function formatDuration(milliseconds: number | null | undefined): string {
  if (milliseconds == null) return EM_DASH;
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

/** Render a governed measurement compactly, honouring its real unit.
 *
 * The backend permits provider-specific scalars in several numeric domains
 * (see `measurement_semantics.py`): durations in `ms`/`s`, dimensionless
 * ratios/probabilities as OpenTelemetry unit `"1"`, counters as `count`, plus
 * arbitrary provider units (`dbfs`, `{character}`, …). Formatting every scalar
 * as milliseconds silently mislabels the non-duration domains, so this
 * dispatches on the unit instead of assuming latency. */
export function formatMeasurement(
  value: boolean | number | null | undefined,
  unit: string | null | undefined,
): string {
  if (value == null) return EM_DASH;
  if (typeof value === "boolean") return value ? "yes" : "no";
  const u = (unit ?? "").trim();
  if (u === "ms") return formatDuration(value);
  if (u === "s") return formatDuration(value * 1000);
  // OpenTelemetry marks a dimensionless ratio/probability with unit "1"; the
  // bare number reads truer than "0.5 1".
  if (u === "1" || u === "") return trimNumber(value);
  // count/dbfs/{character}/… carry a real unit — keep the value paired with it,
  // stripping the OTel annotation braces (`{character}` -> `character`).
  const label = u.replace(/^\{(.*)\}$/, "$1");
  return `${trimNumber(value)} ${label}`;
}

/** A finite number without trailing-zero noise (integers stay integers). */
function trimNumber(value: number): string {
  if (!Number.isFinite(value)) return EM_DASH;
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(3)));
}

/** A coarse "time ago" for a unix-nanosecond timestamp string (BigInt-safe). */
export function formatRelativeTime(unixNano: string, now: number = Date.now()): string {
  const seconds = Number(BigInt(unixNano) / 1_000_000_000n);
  const diff = Math.max(0, Math.floor(now / 1000) - seconds);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86_400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86_400)}d ago`;
}
