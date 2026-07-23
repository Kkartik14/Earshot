export type Tone = "good" | "crit" | "warn" | "muted";

/** Map a session/operation status to a semantic tone for dots, pills, chips. */
export function statusTone(status: string): Tone {
  switch (status) {
    case "completed":
      return "good";
    case "failed":
    case "error":
      return "crit";
    case "timed_out":
    case "timeout":
    case "processing":
    case "in_progress":
    case "cancelled":
    case "canceled":
      return "warn";
    default:
      return "muted";
  }
}

/** The themed CSS custom property that paints a given tone. */
export function toneColorVar(tone: Tone): string {
  switch (tone) {
    case "good":
      return "var(--good)";
    case "crit":
      return "var(--crit)";
    case "warn":
      return "var(--est)";
    default:
      return "var(--tx-low)";
  }
}
