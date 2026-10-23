import type { AudibleGap, CallCost } from "./api";

const EM_DASH = "—";
const NANO_PER_USD = 1_000_000_000;

/** A nano-USD amount in dollars: cents from $1 up, three significant digits below
 * (voice calls cost fractions of a cent), and an exact zero as `$0`. */
export function formatUsd(nanoUsd: number): string {
  if (nanoUsd === 0) return "$0";
  const dollars = nanoUsd / NANO_PER_USD;
  if (Math.abs(dollars) >= 1) {
    return `$${dollars.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }
  const digits = Math.abs(dollars) < 1e-6 ? dollars.toFixed(9) : dollars.toPrecision(3);
  return `$${digits.includes(".") ? digits.replace(/0+$/, "").replace(/\.$/, "") : digits}`;
}

/** A call's cost as the list and the call view show it: the amount when every
 * item is known, else a lower bound with the count of unknown items; the
 * estimate is never added into it. A call with no cost item shows a dash. */
export function costReadout(cost: CallCost): { amount: string; estimate: string | null } {
  const lower = cost.lower_bound_nano_usd;
  const amount =
    lower == null
      ? EM_DASH
      : cost.complete
        ? formatUsd(lower)
        : `≥ ${formatUsd(lower)} · ${cost.unknown_items} unknown`;
  const estimate =
    cost.estimated_nano_usd == null
      ? null
      : `+ ~${formatUsd(cost.estimated_nano_usd)} (estimated)`;
  return { amount, estimate };
}

/** Milliseconds as seconds with two decimals; unknown as a dash, never 0. */
export function formatSeconds(milliseconds: number | null | undefined): string {
  return milliseconds == null ? EM_DASH : `${(milliseconds / 1000).toFixed(2)} s`;
}

const GAP_REASONS: Record<NonNullable<AudibleGap["reason"]>, string> = {
  recorder_timeline_discontinuity:
    "the recorder's timeline jumps inside this turn, so only an upper bound is known",
  no_agent_audio: "no agent speech was found after the user's turn",
  no_clear_pairing: "the user's and the agent's speech could not be paired",
  not_one_timeline: "the user's and the agent's recordings are not on one timeline",
  no_recording: "no recording covers this turn",
  analysis_not_stored:
    "the audio analysis is not stored yet (pending, failed or unavailable)",
  boundary_uncertainty_too_high:
    "the speech edges are too uncertain to report a gap within 100 ms",
};

/** The audible gap at the agent: `value ± uncertainty`, "talk-over", or for a
 * gap that could not be measured its bound (`≤ x s`) or a dash, with the reason. */
export function gapReadout(gap: AudibleGap): { value: string; reason: string | null } {
  if (gap.status === "measured" && gap.value_ms != null) {
    const plusMinus =
      gap.uncertainty_ms == null
        ? ""
        : // Rounded to 10 ms in integers: toFixed(2) on 0.075 s gives 0.07.
          ` ± ${(Math.round(gap.uncertainty_ms / 10) / 100).toFixed(2)}`;
    return { value: `${formatSeconds(gap.value_ms)}${plusMinus}`, reason: null };
  }
  if (gap.status === "talk_over") {
    return {
      value: "talk-over",
      reason: "the agent started while the user was speaking",
    };
  }
  return {
    value: gap.bound_ms == null ? EM_DASH : `≤ ${formatSeconds(gap.bound_ms)}`,
    reason: gap.reason == null ? "not measurable" : GAP_REASONS[gap.reason],
  };
}

/** A time on the call axis as a clock (`m:ss.s`, `h:mm:ss.s` from an hour). Axis
 * times may be negative: evidence from before the earliest recording. */
export function formatClock(milliseconds: number): string {
  const sign = milliseconds < 0 ? "-" : "";
  const tenths = Math.floor(Math.abs(milliseconds) / 100);
  const hours = Math.floor(tenths / 36_000);
  const minutes = Math.floor((tenths % 36_000) / 600);
  const seconds = ((tenths % 600) / 10).toFixed(1).padStart(4, "0");
  return hours > 0
    ? `${sign}${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${sign}${minutes}:${seconds}`;
}

/** A word for an enum value (`not_one_timeline` → "not one timeline"). */
export const humanize = (value: string): string => value.replace(/_/g, " ");
