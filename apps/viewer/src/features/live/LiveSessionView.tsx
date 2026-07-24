import { useId } from "react";
import { Link, useParams } from "react-router-dom";
import { useSessionIncidents } from "../../api/hooks";
import { EmptyState } from "../../components/EmptyState";
import { LiveBanner, standingOf } from "./LiveBanner";
import type { LiveFacts, LiveOpenOperation } from "./liveStore";
import { useSessionTail } from "./useSessionTail";
import type { TailOptions } from "../../api/tail";
import styles from "./LiveSessionView.module.css";

const humanize = (value: string) => value.replace(/_/g, " ");

/** What each unknown is called in the UI. A code with no entry still renders,
 *  humanized — a new server-side unknown must never fall silently off the list. */
const UNKNOWN_LABELS: Record<string, string> = {
  session_status: "Session status",
  session_ended_at: "Session end",
  session_duration: "Duration",
  manifest_finality: "Finality",
  manifest_completeness: "Completeness",
  privacy_manifest: "Privacy manifest",
  turn_membership: "Turns",
  turn_metrics: "Turn metrics, including p95 first-token",
  interruption_classification: "Interruptions",
  derived_analysis: "Derived analysis",
  diagnoses: "Diagnoses",
};

const UNKNOWN_REASONS: Record<string, string> = {
  derived_analysis:
    "analysis binds to the digest of a finished artifact; there is none yet",
  diagnoses:
    "a diagnosis is analyzer output, and the analyzer cannot run on a moving target",
  turn_metrics:
    "a turn is not complete until the session is; more operations may still join it",
  interruption_classification:
    "classification needs the whole chain, and a missing stage is indistinguishable from one that has not arrived",
  privacy_manifest:
    "which capture classes were actually retained is only settled at close",
};

const DEFAULT_REASON = "available after the session closes";

/** One admitted-fact counter. A count of what has arrived so far, and labelled
 *  as exactly that — never presented as a total. */
function FactCount({ kind, count }: { kind: string; count: number }) {
  return (
    <div className={styles.count}>
      <span className={styles.countValue}>{count}</span>
      <span className={styles.countLabel}>{humanize(kind)}</span>
    </div>
  );
}

/** An operation the journal saw start and has not seen end.
 *
 *  The bar is drawn with an open, hatched right edge and carries no width that
 *  could be read as a duration: the start was observed, the end genuinely was
 *  not, and there is nothing in between to measure. */
function OpenOperation({ operation }: { operation: LiveOpenOperation }) {
  return (
    <li className={styles.openOp}>
      <span className={styles.openBar} aria-hidden="true" />
      <span className={styles.openName}>{operation.operationName}</span>
      <span className={styles.openMeta}>
        {operation.turnId == null ? "no turn assigned yet" : operation.turnId}
      </span>
      <span className={styles.openUnknown}>no end observed</span>
    </li>
  );
}

export function LiveSessionBody({
  sessionId,
  tailOptions,
}: {
  sessionId: string;
  tailOptions?: Pick<TailOptions, "eventSourceFactory" | "staleAfterMs" | "maxErrors">;
}) {
  const { facts, connection, silentForMs, reconnect } = useSessionTail(
    sessionId,
    tailOptions,
  );
  const standing = standingOf(facts, connection);
  const bannerId = useId();
  // Only look for an artifact once the stream says the session is over. Polling
  // earlier would invite reading a half-written session as a finished one.
  const settled = facts.closeObserved || facts.ending != null;
  const stored = useSessionIncidents(sessionId, { enabled: settled, pollMs: 3_000 });
  const artifact = stored.data?.items?.[0];

  return (
    <div className={styles.view} aria-describedby={bannerId}>
      <LiveBanner
        facts={facts}
        connection={connection}
        silentForMs={silentForMs}
        headingId={bannerId}
      />

      <header className={styles.head}>
        <h1 className={styles.id}>{facts.sessionId ?? sessionId}</h1>
        <span className={styles.chip}>
          {facts.producer == null
            ? "producer not yet declared"
            : `${facts.producer.name} ${facts.producer.version}`}
        </span>
        {facts.capturePolicy == null ? null : (
          <span className={styles.chip}>
            policy {facts.capturePolicy.policyId} {facts.capturePolicy.policyVersion}
          </span>
        )}
      </header>

      {settled ? (
        <section className={styles.panel} aria-label="Final artifact">
          <div className={styles.panelHead}>
            <h2>Final artifact</h2>
          </div>
          {artifact == null ? (
            <p className={styles.note}>
              {facts.closeObserved
                ? "The recorder closed. Waiting for the immutable artifact to be stored; it is delivered separately and is not on this stream."
                : "No artifact has been stored for this session. If the process ended before close, one can be recovered from its checkpoint journal — this view will not invent it."}
            </p>
          ) : (
            <>
              <p className={styles.note}>
                An artifact for this session is stored. Opening it replaces this partial
                account with the evidence the producer actually attested.
              </p>
              <Link
                className={styles.action}
                to={`/sessions/${encodeURIComponent(artifact.bundle_id)}`}
              >
                Show the final artifact
                {artifact.finality === "final" ? "" : ` (${artifact.finality})`}
              </Link>
            </>
          )}
        </section>
      ) : null}

      {connection.kind === "closed" ? (
        <section className={styles.panel} aria-label="Connection">
          <div className={styles.panelHead}>
            <h2>Connection</h2>
            <span className={styles.note}>{humanize(connection.reason)}</span>
          </div>
          <p className={styles.note}>
            Nothing was dropped: the durable journal still holds every record, and
            reconnecting resumes at the last record this page received.
          </p>
          <button type="button" className={styles.action} onClick={reconnect}>
            Reconnect and catch up
          </button>
        </section>
      ) : null}

      <section className={styles.panel} aria-label="Not knowable yet">
        <div className={styles.panelHead}>
          <h2>Not knowable yet</h2>
          <span className={styles.note}>
            {standing === "closed_awaiting_artifact"
              ? "settled at close; read them from the artifact"
              : "these are absent, not zero"}
          </span>
        </div>
        <dl className={styles.unknowns}>
          {(facts.unknownUntilClose.length > 0
            ? facts.unknownUntilClose
            : Object.keys(UNKNOWN_LABELS)
          ).map((code) => (
            <div key={code} className={styles.unknownRow}>
              <dt className={styles.unknownName}>
                {UNKNOWN_LABELS[code] ?? humanize(code)}
              </dt>
              <dd className={styles.unknownValue}>
                unknown
                <span className={styles.unknownReason}>
                  {UNKNOWN_REASONS[code] ?? DEFAULT_REASON}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section className={styles.panel} aria-label="Operations in progress">
        <div className={styles.panelHead}>
          <h2>Operations in progress</h2>
          <span className={styles.note}>{facts.openOperations.length}</span>
        </div>
        {facts.openOperations.length === 0 ? (
          <p className={styles.note}>
            No operation is currently open in the records received so far. That is not a
            claim that none is running.
          </p>
        ) : (
          <ul className={styles.openList}>
            {facts.openOperations.map((operation) => (
              <OpenOperation key={operation.operationId} operation={operation} />
            ))}
          </ul>
        )}
      </section>

      <section className={styles.panel} aria-label="Admitted facts">
        <div className={styles.panelHead}>
          <h2>Admitted facts</h2>
          <span className={styles.note}>so far, in journal order</span>
        </div>
        {Object.keys(facts.recordCounts).length === 0 ? (
          <p className={styles.note}>No records have been admitted yet.</p>
        ) : (
          <div className={styles.counts}>
            {Object.entries(facts.recordCounts)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([kind, count]) => (
                <FactCount key={kind} kind={kind} count={count} />
              ))}
          </div>
        )}
      </section>

      {facts.restriction.withheldRecords > 0 ||
      facts.restriction.declaredClasses.length > 0 ||
      !facts.restriction.policyReadable ? (
        <section className={styles.panel} aria-label="Restricted by export policy">
          <div className={styles.panelHead}>
            <h2>Restricted by export policy</h2>
            <span className={styles.note}>
              {facts.restriction.withheldRecords} withheld
            </span>
          </div>
          <p className={styles.note}>
            This stream is an export
            {facts.restriction.destination == null
              ? ""
              : ` to ${humanize(facts.restriction.destination)}`}
            , and the capture policy forbids that destination carrying some of what this
            session recorded. The records still exist and still occupy their journal
            slots; their content is not here, and this view is not a complete account of
            the session.
          </p>
          {facts.restriction.policyReadable ? null : (
            <p className={styles.note}>
              The server could not read this session&apos;s export policy and withheld
              everything rather than guess.
            </p>
          )}
          {facts.restriction.refusals.length > 0 ? (
            <ul className={styles.limitList}>
              {facts.restriction.refusals.map((refusal) => (
                <li
                  key={`${refusal.captureClass ?? "unreadable"}:${refusal.reason}`}
                  className={styles.limitRow}
                >
                  <span className={styles.limitReason}>
                    {refusal.captureClass == null
                      ? "policy unreadable"
                      : humanize(refusal.captureClass)}
                  </span>
                  <span className={styles.note}>{humanize(refusal.reason)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.note}>
              Declared on open:{" "}
              {facts.restriction.declaredClasses.map(humanize).join(", ")}. Nothing has
              been withheld yet, because none of those classes has been captured on this
              stream.
            </p>
          )}
        </section>
      ) : null}

      {facts.limits.length > 0 ? (
        <section className={styles.panel} aria-label="Capture limits">
          <div className={styles.panelHead}>
            <h2>Capture limits</h2>
            <span className={styles.note}>{facts.limits.length}</span>
          </div>
          <ul className={styles.limitList}>
            {facts.limits.map((limit) => (
              <li key={limit.sequence} className={styles.limitRow}>
                <span className={styles.limitReason}>{humanize(limit.reason)}</span>
                <span className={styles.note}>
                  {humanize(limit.kind)} · {humanize(limit.captureClass)}
                  {limit.wholeRecord ? " · whole record omitted" : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

export function LiveSessionView(props: {
  tailOptions?: Pick<TailOptions, "eventSourceFactory" | "staleAfterMs" | "maxErrors">;
}) {
  const { sessionId } = useParams<{ sessionId: string }>();
  if (sessionId == null) {
    return <EmptyState title="No live session selected" />;
  }
  return <LiveSessionBody sessionId={sessionId} tailOptions={props.tailOptions} />;
}

export type { LiveFacts };
