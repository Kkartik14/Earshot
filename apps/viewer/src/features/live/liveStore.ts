import type { TailEvent } from "../../api/tail";

/** One operation the journal saw start and has not seen end.
 *
 *  It is a distinct shape from a completed operation on purpose. There is no
 *  `endedAt` and no `durationNano` field to accidentally fill in, so nothing
 *  downstream can render an extrapolated width or a ticking "duration" that
 *  would read as a measurement. */
export interface LiveOpenOperation {
  sequence: number;
  operationId: string;
  operationName: string;
  turnId: string | null;
  participantId: string | null;
  startedAtNano: string | null;
  clockDomainId: string | null;
}

/** One admitted contract record, exactly as the journal holds it. */
export interface LiveRecord {
  sequence: number;
  kind: string;
  value: Record<string, unknown> | null;
}

/** One limit the recorder hit: something was not captured, and this is why. */
export interface LiveLimit {
  sequence: number;
  reason: string;
  kind: string;
  captureClass: string;
  wholeRecord: boolean;
}

export interface LiveTruncation {
  reason: string;
  withheldRecords: number;
  availableFromSequence: number;
}

/** What this stream is not allowed to carry, and how much it has refused.
 *
 *  The tail is an export, so the server reapplies its destination policy before
 *  a record leaves the process. A record it may not carry still occupies its
 *  journal slot as a `withheld` event, and this is what keeps "restricted by
 *  policy" distinguishable from "nothing was recorded". */
export interface LiveRestriction {
  /** The export destination this stream declares itself to be. */
  destination: string | null;
  /** Declared on `open`: enabled classes whose policy forbids that destination. */
  declaredClasses: string[];
  /** False when the server could not read the policy and withheld everything. */
  policyReadable: boolean;
  /** Records withheld so far. What was refused on this connection, not a total. */
  withheldRecords: number;
  /** Every class that has actually refused, with its reason, deduplicated. */
  refusals: { captureClass: string | null; reason: string }[];
}

/** Why this stream stopped, when it has. */
export interface LiveEnding {
  reason: string;
  closeObserved: boolean;
}

export interface LiveFinalize {
  status: string;
  journalComplete: boolean;
  firstLimitReason: string | null;
  truncatedRecords: number;
}

/** Everything the viewer knows about a session that has not closed.
 *
 *  Deliberately not shaped like an incident: there is no manifest, no session
 *  status, no coverage roll-up and no metric anywhere in this type, because none
 *  of those are knowable before close. `unknownUntilClose` is the server's own
 *  enumeration of that, carried through so the UI states it rather than
 *  implying it by omission. */
export interface LiveFacts {
  journalId: string | null;
  sessionId: string | null;
  bundleId: string | null;
  source: string | null;
  producer: { name: string; version: string } | null;
  capturePolicy: {
    policyId: string;
    policyVersion: string;
    enabledClasses: string[];
  } | null;
  startedAtNano: string | null;
  unknownUntilClose: string[];
  asOfSequence: number;
  recordCounts: Record<string, number>;
  records: LiveRecord[];
  openOperations: LiveOpenOperation[];
  completedOperationIds: string[];
  limits: LiveLimit[];
  restriction: LiveRestriction;
  truncation: LiveTruncation | null;
  /** True only once the journal reported its own cap; never inferred. */
  journalExhausted: boolean;
  closeObserved: boolean;
  finalize: LiveFinalize | null;
  overflowed: boolean;
  ending: LiveEnding | null;
  /** How many events this view has applied. Diagnostics, not evidence. */
  appliedEvents: number;
}

/** Cap on retained records so a long session cannot grow the tab without bound.
 *  Dropping the oldest is safe here and only here: the durable journal still
 *  holds them, and the artifact will carry all of them. */
const MAX_RETAINED_RECORDS = 2_000;

export function emptyFacts(): LiveFacts {
  return {
    journalId: null,
    sessionId: null,
    bundleId: null,
    source: null,
    producer: null,
    capturePolicy: null,
    startedAtNano: null,
    unknownUntilClose: [],
    asOfSequence: 0,
    recordCounts: {},
    records: [],
    openOperations: [],
    completedOperationIds: [],
    limits: [],
    restriction: {
      destination: null,
      declaredClasses: [],
      policyReadable: true,
      withheldRecords: 0,
      refusals: [],
    },
    truncation: null,
    journalExhausted: false,
    closeObserved: false,
    finalize: null,
    overflowed: false,
    ending: null,
    appliedEvents: 0,
  };
}

const str = (value: unknown): string | null => (typeof value === "string" ? value : null);
const num = (value: unknown): number => (typeof value === "number" ? value : 0);
const bool = (value: unknown): boolean => value === true;
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

function record(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Apply one tail event. Pure, total, and never throws on an unexpected shape.
 *
 *  Ordering and de-duplication both come from the journal sequence rather than
 *  from arrival order: a reconnect can only ever replay, never reorder, so an
 *  event at or below `asOfSequence` has already been applied and is dropped. */
export function applyEvent(facts: LiveFacts, event: TailEvent): LiveFacts {
  if (event.name === "reset") {
    // A different journal for the same session. Everything held is about the
    // previous one and would be a splice of two conversations if kept.
    return { ...emptyFacts(), appliedEvents: facts.appliedEvents + 1 };
  }
  if (event.sequence != null && event.sequence <= facts.asOfSequence) {
    return facts;
  }
  const next: LiveFacts = {
    ...facts,
    appliedEvents: facts.appliedEvents + 1,
    asOfSequence: event.sequence ?? facts.asOfSequence,
    journalId: event.journalId ?? facts.journalId,
  };

  switch (event.name) {
    case "open": {
      const producer = record(event.data.producer);
      const policy = record(event.data.capture_policy);
      const startedAt = record(event.data.started_at);
      next.sessionId = str(event.data.session_id);
      next.bundleId = str(event.data.bundle_id);
      next.source = str(event.data.source);
      next.journalId = str(event.data.journal_id) ?? next.journalId;
      next.producer =
        producer == null
          ? null
          : { name: str(producer.name) ?? "", version: str(producer.version) ?? "" };
      next.capturePolicy =
        policy == null
          ? null
          : {
              policyId: str(policy.policy_id) ?? "",
              policyVersion: str(policy.policy_version) ?? "",
              enabledClasses: Array.isArray(policy.enabled_classes)
                ? policy.enabled_classes.filter(
                    (item): item is string => typeof item === "string",
                  )
                : [],
            };
      next.startedAtNano = startedAt == null ? null : str(startedAt.monotonic_time_nano);
      next.unknownUntilClose = strings(event.data.unknown_until_close);
      const exportPolicy = record(event.data.export_policy);
      next.restriction = {
        ...facts.restriction,
        destination: exportPolicy == null ? null : str(exportPolicy.destination),
        declaredClasses:
          exportPolicy == null ? [] : strings(exportPolicy.denied_capture_classes),
        // Absent rather than false when the server never said: an older backend
        // that does not declare this has not declared an unreadable policy.
        policyReadable: exportPolicy == null || exportPolicy.policy_readable !== false,
      };
      return next;
    }
    case "record": {
      const kind = str(event.data.kind) ?? "unknown";
      const value = record(event.data.value);
      next.recordCounts = {
        ...facts.recordCounts,
        [kind]: (facts.recordCounts[kind] ?? 0) + 1,
      };
      const entry: LiveRecord = { sequence: event.sequence ?? 0, kind, value };
      const retained = [...facts.records, entry];
      next.records =
        retained.length > MAX_RETAINED_RECORDS
          ? retained.slice(retained.length - MAX_RETAINED_RECORDS)
          : retained;
      if (kind === "operation") {
        const operationId = value == null ? null : str(value.operation_id);
        if (operationId != null) {
          next.completedOperationIds = [...facts.completedOperationIds, operationId];
          next.openOperations = facts.openOperations.filter(
            (open) => open.operationId !== operationId,
          );
        }
      }
      return next;
    }
    case "withheld": {
      // Counted, never reconstructed. The slot is accounted for and its content
      // is not here, which is the whole point of the event existing.
      const refusals = [...facts.restriction.refusals];
      for (const denial of Array.isArray(event.data.denied_capture_classes)
        ? event.data.denied_capture_classes
        : []) {
        const entry = record(denial);
        if (entry == null) continue;
        const captureClass = str(entry.capture_class);
        const reason = str(entry.reason) ?? "unknown";
        if (
          !refusals.some(
            (seen) => seen.captureClass === captureClass && seen.reason === reason,
          )
        ) {
          refusals.push({ captureClass, reason });
        }
      }
      next.restriction = {
        ...facts.restriction,
        destination: str(event.data.destination) ?? facts.restriction.destination,
        withheldRecords: facts.restriction.withheldRecords + 1,
        refusals,
      };
      return next;
    }
    case "operation_open": {
      const operationId = str(event.data.operation_id);
      if (operationId == null) return next;
      if (facts.completedOperationIds.includes(operationId)) return next;
      const startedAt = record(event.data.started_at);
      next.openOperations = [
        ...facts.openOperations.filter((open) => open.operationId !== operationId),
        {
          sequence: event.sequence ?? 0,
          operationId,
          operationName: str(event.data.operation_name) ?? "operation",
          turnId: str(event.data.turn_id),
          participantId: str(event.data.participant_id),
          startedAtNano: startedAt == null ? null : str(startedAt.monotonic_time_nano),
          clockDomainId: startedAt == null ? null : str(startedAt.clock_domain_id),
        },
      ];
      return next;
    }
    case "limit": {
      next.limits = [
        ...facts.limits,
        {
          sequence: event.sequence ?? 0,
          reason: str(event.data.reason) ?? "unknown",
          kind: str(event.data.kind) ?? "unknown",
          captureClass: str(event.data.capture_class) ?? "unknown",
          wholeRecord: bool(event.data.whole_record),
        },
      ];
      return next;
    }
    case "exhausted": {
      next.journalExhausted = true;
      return next;
    }
    case "finalize": {
      next.closeObserved = true;
      next.finalize = {
        status: str(event.data.status) ?? "unknown",
        journalComplete: bool(event.data.journal_complete),
        firstLimitReason: str(event.data.first_limit_reason),
        truncatedRecords: num(event.data.truncated_records),
      };
      return next;
    }
    case "replay_truncated": {
      next.truncation = {
        reason: str(event.data.reason) ?? "unknown",
        withheldRecords: num(event.data.withheld_records),
        availableFromSequence: num(event.data.available_from_sequence),
      };
      return next;
    }
    case "overflow": {
      next.overflowed = true;
      return next;
    }
    case "end": {
      next.ending = {
        reason: str(event.data.reason) ?? "unknown",
        closeObserved: bool(event.data.close_observed),
      };
      return next;
    }
    default:
      return next;
  }
}

type Listener = () => void;

/** A tiny external store for `useSyncExternalStore`.
 *
 *  React Query is request/response and models a stream badly; a reducer over an
 *  append-only log is the shape the wire already has. */
export class LiveStore {
  private facts = emptyFacts();
  private readonly listeners = new Set<Listener>();

  getSnapshot = (): LiveFacts => this.facts;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  apply(event: TailEvent): void {
    const next = applyEvent(this.facts, event);
    if (next === this.facts) return;
    this.facts = next;
    for (const listener of this.listeners) listener();
  }

  clear(): void {
    this.facts = emptyFacts();
    for (const listener of this.listeners) listener();
  }
}
