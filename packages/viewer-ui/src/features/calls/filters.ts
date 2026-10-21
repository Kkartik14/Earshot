import type { CallListFilters } from "./api";

/** The calls list's filter form, as typed. */
export interface CallFilterForm {
  agent: string;
  outcome: string;
  /** Comma-separated; a call must carry every one. */
  tags: string;
  minLatencyMs: string;
  maxLatencyMs: string;
  minGapMs: string;
  maxGapMs: string;
  minCostUsd: string;
  maxCostUsd: string;
  /** Transcript text. */
  text: string;
}

export type FilterErrors = Partial<Record<keyof CallFilterForm, string>>;

export const EMPTY_FILTERS: CallFilterForm = {
  agent: "",
  outcome: "",
  tags: "",
  minLatencyMs: "",
  maxLatencyMs: "",
  minGapMs: "",
  maxGapMs: "",
  minCostUsd: "",
  maxCostUsd: "",
  text: "",
};

// The server's limits (`GET /v1/calls`), checked here so a typo is explained in
// place instead of coming back as a 422.
const MAX_TAGS = 10;
const MIN_TEXT = 3;
const MAX_TEXT = 256;
const NANO_PER_USD = 1_000_000_000n;

function milliseconds(text: string): number | string {
  if (!/^\d+$/.test(text)) return "Enter whole milliseconds, 0 or more.";
  const value = Number(text);
  return Number.isSafeInteger(value) ? value : "That number is too large.";
}

/** Dollars, written as a decimal with at most 9 places, to nano-USD exactly. */
function nanoUsd(text: string): number | string {
  const match = /^(\d+)(?:\.(\d{1,9}))?$/.exec(text);
  if (match == null) return "Enter dollars, such as 0.05 (at most 9 decimals).";
  const [, whole, fraction = ""] = match;
  const value = BigInt(whole) * NANO_PER_USD + BigInt(fraction.padEnd(9, "0"));
  return value <= BigInt(Number.MAX_SAFE_INTEGER)
    ? Number(value)
    : "That amount is too large.";
}

/** The API query for a form, or what is wrong with each field. Empty fields are
 * left out; only fields without an error are sent. */
export function filtersToQuery(form: CallFilterForm): {
  query: CallListFilters;
  errors: FilterErrors;
} {
  const query: CallListFilters = {};
  const errors: FilterErrors = {};

  const agent = form.agent.trim();
  if (agent) query.agent = agent;
  const outcome = form.outcome.trim();
  if (outcome) query.outcome = outcome;

  const tags = [
    ...new Set(
      form.tags
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean),
    ),
  ];
  if (tags.length > MAX_TAGS) errors.tags = `At most ${MAX_TAGS} tags.`;
  else if (tags.length > 0) query.tag = tags;

  const numeric = (
    field: keyof CallFilterForm,
    parse: (text: string) => number | string,
  ): number | undefined => {
    const text = form[field].trim();
    if (!text) return undefined;
    const value = parse(text);
    if (typeof value === "string") {
      errors[field] = value;
      return undefined;
    }
    return value;
  };
  const range = (
    min: keyof CallFilterForm,
    max: keyof CallFilterForm,
    parse: (text: string) => number | string,
  ): [number | undefined, number | undefined] => {
    const low = numeric(min, parse);
    const high = numeric(max, parse);
    if (low != null && high != null && low > high) {
      errors[max] = "The maximum is below the minimum.";
      return [undefined, undefined];
    }
    return [low, high];
  };

  const [minLatency, maxLatency] = range("minLatencyMs", "maxLatencyMs", milliseconds);
  if (minLatency != null) query.min_reported_latency_ms = minLatency;
  if (maxLatency != null) query.max_reported_latency_ms = maxLatency;
  const [minGap, maxGap] = range("minGapMs", "maxGapMs", milliseconds);
  if (minGap != null) query.min_audible_gap_ms = minGap;
  if (maxGap != null) query.max_audible_gap_ms = maxGap;
  const [minCost, maxCost] = range("minCostUsd", "maxCostUsd", nanoUsd);
  if (minCost != null) query.min_cost_nano_usd = minCost;
  if (maxCost != null) query.max_cost_nano_usd = maxCost;

  const text = form.text.trim();
  if (text.length > 0 && text.length < MIN_TEXT) {
    errors.text = `Enter at least ${MIN_TEXT} characters.`;
  } else if (text.length > MAX_TEXT) {
    errors.text = `Enter at most ${MAX_TEXT} characters.`;
  } else if (text) {
    query.q = text;
  }

  return { query, errors };
}

/** Whether any filter is in effect (an empty result then means "no match"). */
export const hasFilters = (query: CallListFilters): boolean =>
  Object.keys(query).length > 0;
