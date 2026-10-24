import { useId, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { EmptyState } from "../../components/EmptyState";
import { formatRelativeTime } from "../../lib/format";
import { AnchorLink, type ViewerLinkComponent } from "../../navigation";
import { useCalls, type CallListFilters, type CallSummary } from "./api";
import {
  EMPTY_FILTERS,
  filtersToQuery,
  hasFilters,
  type CallFilterForm,
  type FilterErrors,
} from "./filters";
import { costReadout, formatSeconds } from "./format";
import styles from "./CallsList.module.css";

const PRESET_FORBIDS_SEARCH = "EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH";
const KNOWN_OUTCOMES = ["completed", "interrupted", "error"];

type FieldProps = {
  field: keyof CallFilterForm;
  label: string;
  form: CallFilterForm;
  errors: FilterErrors;
  onChange: (field: keyof CallFilterForm, value: string) => void;
  inputMode?: "decimal" | "numeric";
  list?: string;
  placeholder?: string;
};

function Field({
  field,
  label,
  form,
  errors,
  onChange,
  inputMode,
  list,
  placeholder,
}: FieldProps) {
  const id = useId();
  const error = errors[field];
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={form[field]}
        onChange={(event) => onChange(field, event.target.value)}
        inputMode={inputMode}
        list={list}
        placeholder={placeholder}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        spellCheck={false}
        autoComplete="off"
      />
      {error ? (
        <span id={`${id}-error`} className={styles.fieldError}>
          {error}
        </span>
      ) : null}
    </div>
  );
}

function ProjectionCells({ call }: { call: CallSummary }) {
  const projection = call.projection;
  if (projection == null) {
    // Unknown is never drawn as zeros: an unprojected call has no metrics yet.
    return (
      <td colSpan={7} className={styles.dim}>
        not projected yet{call.state === "open" ? " (the call is still open)" : ""}
      </td>
    );
  }
  const cost = costReadout(projection.cost);
  return (
    <>
      <td>{projection.agent ?? "—"}</td>
      <td>{projection.outcome ?? "—"}</td>
      <td className={styles.num}>{formatSeconds(projection.duration_ms)}</td>
      <td className={styles.num}>
        {projection.turn_count}
        <span className={styles.dim}> ({projection.measurable_turns} measurable)</span>
      </td>
      <td className={styles.num}>{formatSeconds(projection.reported_latency_p50_ms)}</td>
      <td className={styles.num}>{formatSeconds(projection.audible_gap_p50_ms)}</td>
      <td className={styles.num}>
        {cost.amount}
        {cost.estimate ? <span className={styles.estimate}>{cost.estimate}</span> : null}
      </td>
    </>
  );
}

function CallsTable({
  calls,
  LinkComponent,
}: {
  calls: CallSummary[];
  LinkComponent: ViewerLinkComponent;
}) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Calls, newest first. Latency, gap and cost are each call&apos;s values as
          projected; p50 over its turns.
        </caption>
        <thead>
          <tr>
            <th scope="col">Call</th>
            <th scope="col">Started</th>
            <th scope="col">Agent</th>
            <th scope="col">Outcome</th>
            <th scope="col" className={styles.num}>
              Duration
            </th>
            <th scope="col" className={styles.num}>
              Turns
            </th>
            <th scope="col" className={styles.num}>
              Reported latency p50
            </th>
            <th scope="col" className={styles.num}>
              Audible gap p50
            </th>
            <th scope="col" className={styles.num}>
              Cost
            </th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call) => (
            <tr key={call.call_id}>
              <th scope="row" className={styles.callCell}>
                <LinkComponent
                  href={`/calls/${encodeURIComponent(call.call_id)}`}
                  className={styles.callLink}
                >
                  {call.session_key ?? call.call_id}
                </LinkComponent>
                {call.projection?.tags.map((tag) => (
                  <span key={tag} className={styles.tag}>
                    {tag}
                  </span>
                ))}
              </th>
              <td className={styles.nowrap}>
                {formatRelativeTime(call.first_evidence_unix_nano)}
              </td>
              <ProjectionCells call={call} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Every call of the project, newest first, with filters on the projection:
 * agent, outcome, tags, reported latency, audible gap, cost and transcript text. */
export function CallsList({
  LinkComponent = AnchorLink,
}: {
  LinkComponent?: ViewerLinkComponent;
}) {
  const [form, setForm] = useState<CallFilterForm>(EMPTY_FILTERS);
  const [errors, setErrors] = useState<FilterErrors>({});
  const [filters, setFilters] = useState<CallListFilters>({});
  const calls = useCalls(filters);
  const outcomesId = useId();

  const change = (field: keyof CallFilterForm, value: string) =>
    setForm((current) => ({ ...current, [field]: value }));
  const apply = (event: FormEvent) => {
    event.preventDefault();
    const next = filtersToQuery(form);
    setErrors(next.errors);
    if (Object.keys(next.errors).length === 0) setFilters(next.query);
  };
  const clear = () => {
    setForm(EMPTY_FILTERS);
    setErrors({});
    setFilters({});
  };

  const items = calls.data?.pages.flatMap((page) => page.items) ?? [];
  // The same on every page: it ignores the cursor.
  const excluded = calls.data?.pages[0]?.excluded_incomplete ?? null;
  const filtered = hasFilters(filters);
  const field = { form, errors, onChange: change };

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className={styles.title}>Calls</h1>
        <p className={styles.subtitle}>
          Filters match each call&apos;s projection; a call that is not projected yet
          matches none of them.
        </p>
        <form
          role="search"
          aria-label="Filter calls"
          className={styles.filters}
          onSubmit={apply}
          noValidate
        >
          <Field {...field} field="agent" label="Agent" />
          <Field
            {...field}
            field="outcome"
            label="Outcome"
            list={outcomesId}
            placeholder="completed"
          />
          <datalist id={outcomesId}>
            {KNOWN_OUTCOMES.map((outcome) => (
              <option key={outcome} value={outcome} />
            ))}
          </datalist>
          <Field
            {...field}
            field="tags"
            label="Tags (all of)"
            placeholder="vip, refund"
          />
          <Field
            {...field}
            field="text"
            label="Transcript text"
            placeholder="window seat"
          />
          <fieldset className={styles.range}>
            <legend>Reported latency p50, ms</legend>
            <Field
              {...field}
              field="minLatencyMs"
              label="Latency at least"
              inputMode="numeric"
            />
            <Field
              {...field}
              field="maxLatencyMs"
              label="Latency at most"
              inputMode="numeric"
            />
          </fieldset>
          <fieldset className={styles.range}>
            <legend>Audible gap p50, ms</legend>
            <Field {...field} field="minGapMs" label="Gap at least" inputMode="numeric" />
            <Field {...field} field="maxGapMs" label="Gap at most" inputMode="numeric" />
          </fieldset>
          <fieldset className={styles.range}>
            <legend>Cost, USD</legend>
            <Field
              {...field}
              field="minCostUsd"
              label="Cost at least"
              inputMode="decimal"
            />
            <Field
              {...field}
              field="maxCostUsd"
              label="Cost at most"
              inputMode="decimal"
            />
          </fieldset>
          <div className={styles.actions}>
            <button type="submit" className={styles.primary}>
              Apply filters
            </button>
            <button type="button" className={styles.secondary} onClick={clear}>
              Clear
            </button>
          </div>
        </form>
      </header>

      <div className={styles.results}>
        {calls.isPending ? (
          <p className={styles.note} role="status">
            Loading calls…
          </p>
        ) : null}

        {calls.isError ? (
          calls.error instanceof ApiError &&
          calls.error.message === PRESET_FORBIDS_SEARCH ? (
            <EmptyState
              title="Transcript search is off for this project"
              hint="Its capture preset (metadata_only) keeps no transcript text, so there is nothing to search. The other filters still work."
            />
          ) : (
            <EmptyState title="Calls are unavailable" hint={calls.error.message} />
          )
        ) : null}

        {calls.isSuccess ? (
          <>
            <p className={styles.count} role="status">
              {items.length === 0
                ? filtered
                  ? "No calls match these filters."
                  : "No calls yet."
                : `Showing ${items.length} call${items.length === 1 ? "" : "s"}${calls.hasNextPage ? "; more are available" : ""}.`}
            </p>
            {excluded != null && excluded > 0 ? (
              <p className={styles.excluded}>
                {excluded} call{excluded === 1 ? "" : "s"} with incomplete cost not shown
              </p>
            ) : null}
            {items.length === 0 && !filtered ? (
              <EmptyState
                title="No calls yet"
                hint="A call appears when your agent sends OTLP spans with a session key to /v1/traces."
              />
            ) : null}
            {items.length > 0 ? (
              <CallsTable calls={items} LinkComponent={LinkComponent} />
            ) : null}
            {calls.hasNextPage ? (
              <button
                type="button"
                className={styles.more}
                onClick={() => void calls.fetchNextPage()}
                disabled={calls.isFetchingNextPage}
              >
                {calls.isFetchingNextPage ? "Loading more…" : "Load more"}
              </button>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
