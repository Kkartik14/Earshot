import { describe, expect, it } from "vitest";
import type { TailEvent } from "../../api/tail";
import { applyEvent, emptyFacts, LiveStore, type LiveFacts } from "./liveStore";

const JOURNAL = "6c64ca59b0544136";

function event(
  name: TailEvent["name"],
  sequence: number | null,
  data: Record<string, unknown> = {},
): TailEvent {
  return { name, sequence, journalId: JOURNAL, data };
}

const OPEN = event("open", 1, {
  journal_id: JOURNAL,
  session_id: "s-1",
  bundle_id: "b-1",
  source: "journal",
  producer: { name: "earshot", version: "0.1.0" },
  capture_policy: {
    policy_id: "default",
    policy_version: "1",
    enabled_classes: ["metadata"],
  },
  started_at: { monotonic_time_nano: "1000", clock_domain_id: "proc" },
  in_progress: true,
  unknown_until_close: ["session_status", "turn_metrics", "derived_analysis"],
});

function reduce(events: TailEvent[], from: LiveFacts = emptyFacts()): LiveFacts {
  return events.reduce(applyEvent, from);
}

describe("liveStore", () => {
  it("carries the header's own list of what cannot be known yet", () => {
    const facts = reduce([OPEN]);
    expect(facts.sessionId).toBe("s-1");
    expect(facts.producer).toEqual({ name: "earshot", version: "0.1.0" });
    expect(facts.unknownUntilClose).toContain("derived_analysis");
    // Nothing is claimed about the session itself.
    expect(facts.closeObserved).toBe(false);
    expect(facts.finalize).toBeNull();
  });

  it("orders by journal sequence and counts records by kind", () => {
    const facts = reduce([
      OPEN,
      event("record", 2, { kind: "participant", value: { participant_id: "p" } }),
      event("record", 3, { kind: "event", value: { event_name: "turn.start" } }),
      event("record", 4, { kind: "event", value: { event_name: "turn.end" } }),
    ]);
    expect(facts.asOfSequence).toBe(4);
    expect(facts.recordCounts).toEqual({ participant: 1, event: 2 });
    expect(facts.records.map((item) => item.sequence)).toEqual([2, 3, 4]);
  });

  it("drops a replayed event rather than counting it twice", () => {
    const once = reduce([OPEN, event("record", 2, { kind: "event", value: {} })]);
    const again = applyEvent(once, event("record", 2, { kind: "event", value: {} }));
    expect(again).toBe(once);
    expect(again.recordCounts).toEqual({ event: 1 });
  });

  it("keeps an unfinished operation with no end and no duration", () => {
    const facts = reduce([
      OPEN,
      event("operation_open", 2, {
        operation_id: "op-1",
        operation_name: "llm",
        turn_id: "turn-0",
        started_at: { monotonic_time_nano: "2000", clock_domain_id: "proc" },
        status: "unknown",
        ended_at: null,
        duration_nano: null,
        end_observed: false,
      }),
    ]);
    expect(facts.openOperations).toHaveLength(1);
    const [operation] = facts.openOperations;
    expect(operation.operationName).toBe("llm");
    expect(operation.startedAtNano).toBe("2000");
    // There is no field that could hold an end or a duration.
    expect(Object.keys(operation)).not.toContain("endedAt");
    expect(Object.keys(operation)).not.toContain("durationNano");
  });

  it("retires an unfinished operation when its completed record arrives", () => {
    const facts = reduce([
      OPEN,
      event("operation_open", 2, {
        operation_id: "op-1",
        operation_name: "llm",
        started_at: { monotonic_time_nano: "2000" },
      }),
      event("record", 3, {
        kind: "operation",
        value: { operation_id: "op-1", operation_name: "llm", status: "ok" },
      }),
    ]);
    expect(facts.openOperations).toHaveLength(0);
    expect(facts.completedOperationIds).toEqual(["op-1"]);
  });

  it("records limits, journal exhaustion and replay truncation as facts", () => {
    const facts = reduce([
      OPEN,
      event("limit", 2, {
        reason: "max_records",
        kind: "event",
        capture_class: "metadata",
        whole_record: true,
      }),
      event("exhausted", 3, { reason: "checkpoint_journal_full" }),
      event("replay_truncated", null, {
        reason: "replay_window_exceeded",
        withheld_records: 12,
        available_from_sequence: 14,
      }),
    ]);
    expect(facts.limits).toHaveLength(1);
    expect(facts.limits[0].reason).toBe("max_records");
    expect(facts.journalExhausted).toBe(true);
    expect(facts.truncation).toEqual({
      reason: "replay_window_exceeded",
      withheldRecords: 12,
      availableFromSequence: 14,
    });
  });

  it("records a close without claiming an artifact exists", () => {
    const facts = reduce([
      OPEN,
      event("finalize", 2, {
        status: "completed",
        journal_complete: true,
        first_limit_reason: null,
        truncated_records: 0,
        artifact_available: false,
      }),
    ]);
    expect(facts.closeObserved).toBe(true);
    expect(facts.finalize?.status).toBe("completed");
    expect(facts.ending).toBeNull();
  });

  it("records an ending that never saw a close as exactly that", () => {
    const facts = reduce([
      OPEN,
      event("end", null, { reason: "journal_removed", close_observed: false }),
    ]);
    expect(facts.ending).toEqual({ reason: "journal_removed", closeObserved: false });
    expect(facts.closeObserved).toBe(false);
  });

  it("clears everything on reset so two journals never splice together", () => {
    const facts = reduce([
      OPEN,
      event("record", 2, { kind: "event", value: {} }),
      event("reset", null, { reason: "journal_identity_changed" }),
    ]);
    expect(facts.records).toHaveLength(0);
    expect(facts.recordCounts).toEqual({});
    expect(facts.sessionId).toBeNull();
    expect(facts.asOfSequence).toBe(0);
  });

  it("flags an overflow so the reader knows the connection, not the evidence, stopped", () => {
    const facts = reduce([
      OPEN,
      event("overflow", null, { reason: "subscriber_fell_behind", last_sequence: 5 }),
    ]);
    expect(facts.overflowed).toBe(true);
  });

  it("survives a malformed payload without inventing a shape for it", () => {
    const facts = reduce([
      OPEN,
      event("record", 2, { kind: 7, value: "not an object" }),
      event("operation_open", 3, { operation_name: "llm" }),
    ]);
    expect(facts.recordCounts).toEqual({ unknown: 1 });
    expect(facts.records[0].value).toBeNull();
    // No operation id means nothing identifiable to track.
    expect(facts.openOperations).toHaveLength(0);
  });

  it("notifies subscribers only when the facts actually change", () => {
    const store = new LiveStore();
    let notifications = 0;
    store.subscribe(() => {
      notifications += 1;
    });
    store.apply(OPEN);
    store.apply(event("record", 2, { kind: "event", value: {} }));
    store.apply(event("record", 2, { kind: "event", value: {} }));
    expect(notifications).toBe(2);
    expect(store.getSnapshot().asOfSequence).toBe(2);
  });
});
