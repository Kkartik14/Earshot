const EM_DASH = "—";

/** A latency in milliseconds, or an em-dash when absent. */
export function formatMs(value: number | null | undefined): string {
  return value == null ? EM_DASH : `${Math.round(value)}ms`;
}

/** A duration given in milliseconds, rendered compactly (ms under 1s, else s). */
export function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
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
