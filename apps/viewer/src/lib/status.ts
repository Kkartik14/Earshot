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
    case "processing":
    case "in_progress":
      return "warn";
    default:
      return "muted";
  }
}
