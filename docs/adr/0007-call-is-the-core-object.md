# ADR 0007: Make the Call the core object, with each Revision an Incident

Status: accepted (2026-10-19)

## Context

Until 0.1.x the Incident was Earshot's core object: a finalized, immutable evidence
artifact for one voice session (ADR 0001). Live OTLP evidence does not arrive finalized.
One call's spans arrive across many requests, recordings arrive after the call, and late
spans arrive after Earshot has decided a call is over. Something must stay open while
evidence arrives, close without a human, and survive restarts.

Three existing objects were candidates. None fits as the owner of call state:

- An **Incident** is immutable by definition, so it cannot be open.
- A **live session** (`LiveSessionRegistry`) is an in-memory, per-process fan-out
  transport, capped at 32 sessions per server and 16 per Project. Its doctrine forbids
  sealing on a timer.
- A browser **CaptureCall** accumulates capture drains into a live session. It is in
  memory and per process, and only an explicit seal materializes it; nothing on a timer
  may finalize it.

Earshot needs closing after a quiet period, durability across restarts, and many open
calls at once.

## Decision

**A Call is a thin, durable aggregate.** It indexes `(Project, Session Key)` to traces,
spans, Recordings and Revision pointers. Spans and stored Recordings are the source of
truth. Revisions are derived from them. The `calls` row holds lifecycle state only:
`state`, `dirty`, `close_due`, the lease, and counts.

- `call_id` is `"call_" + fingerprint("call-session-v1", project, key, generation)[:32]`.
  The generation comes from `call_key_generations` and is 0 when no row exists. The same
  key in two batches gives the same Call.
- A Call is `open` or `closed`. New evidence on a closed Call makes it dirty again.

**Each Revision is an Incident.**

- Revision `n` has bundle ID `<call_id>.r<n>`, and its `session_id` is always `call_id`.
- It is built from **all** of the Call's spans and stored Recordings in the claim
  snapshot. Spans go in `(start_unix_nano, trace_id, span_id)` order to the adapter
  chosen by scope (Pipecat in Month 1). Spans from unknown scopes stay stored, and their
  omission is declared in the bundle.
- `close_reason` is `end_signal`, `quiet_period` or `late_data`. An end-signal close is
  `final`. A quiet-period close is partial: `IncidentRecorder.close_partial` with
  `close_observed=False`, method `otlp_quiet_period`, reason `no_end_signal` and status
  `interrupted`.
- It is deterministic. The same snapshot gives byte-identical artifacts; the manual
  trace ID derives from `call_id` and the revision, and the clock domain is
  `otlp-<call_id>`.
- A Revision is immutable. A later one supersedes it as the Call's latest but never edits
  it.

**The closer.** It is a lifespan task under both `earshot serve` and
`uvicorn apps.ingest.app:app`, with a 1 s tick, split into `claim()`, `materialize()`
and `finish()`.

- **Claim**, under `_mutation()`. A Call is claimable when it is dirty and due, or has a
  `pending_revision`; its lease is absent or expired; and no pending Recording holds it.
  Claim sets `dirty = 0`, `pending_revision = latest + 1`, `lease_until = now + 60 s`,
  increments `lease_token`, and reads the span and Recording IDs as the snapshot.
- **Materialize** builds the bundle between the two transactions.
- **Finish** runs inside `IncidentStore.ingest`'s final catalog transaction. It is
  conditional on `lease_token`: a stale holder's finish matches no row, the transaction
  rolls back, and no Revision is stored. Finish inserts `call_revisions`, sets
  `latest_revision` and `state = 'closed'`, and clears `pending_revision` and the lease.
  It never touches `dirty`, so evidence that arrived after the claim waits for the next
  cycle.
- **Crash recovery.** After a crash following the claim, `pending_revision` keeps the
  Call claimable. After a crash following the catalog commit, nothing is redone, because
  the Revision and the finish committed together. A `call_revisions` row for a still-set
  `pending_revision` is an integrity error (`EARSHOT_CALL_INTEGRITY`): logged, counted,
  and the Call is set aside.
- **Failure.** A failed materialization increments `materialize_attempts`, backs off by
  `min(2^attempts × 1 s, LONG_QUIET)`, and records a stable `materialize_error`.

**Revision cap.** A Call materializes at most 16 Revisions, with one exception: its first
end-signal close is always materialized, so the most is 17. Past the cap, evidence is
still stored and shown, `revision_cap_reached` is counted, and the Call reports
`evidence_after_latest_revision: true`. The claim rule skips a capped Call unless it has
an end signal and no `final` Revision yet.

**Purge, retention and key generations work per Call.**

- `DELETE /v1/calls/{id}` and `earshot call purge` delete the Call row with its spans,
  traces, Recording rows and Revision rows; purge each Revision through the existing
  incident purge; queue blob deletions; and collect unreferenced resources.
- The same transaction increments the key's generation and writes a call tombstone that
  expires after LONG_QUIET + 24 h. While it is live, new evidence for that key is dropped
  and counted. After it expires, the new generation gives a new `call_id`, so a purged
  Call is never rebuilt.
- `DELETE /v1/incidents/{bundle_id}` on a Revision returns 409
  `EARSHOT_INCIDENT_BELONGS_TO_CALL`, with the `call_id`.
- The retention reaper skips Revisions. When a Call's latest Revision expires, the whole
  Call is purged. Open Calls never expire.
- Project deletion covers every call table and the blobs.

**The incident bundle stays the exchange format.** Revisions are ordinary Incidents, so
analysis, turn facts, export and the viewer work on them unchanged. The bundle remains
the import and export format and the input to the analysis engine.

**CaptureCall and live sessions stay as they are.** A CaptureCall is a separate browser
lifecycle that only an explicit seal materializes, and it never becomes a Call. OTLP
Calls are not wired into `LiveSessionRegistry` in Month 1; a live tail is Month 2. Their
patterns may be reused, but they do not own Call state.

## Consequences

Positive:

- Call state survives restarts. Closing is a column and a lease, not an in-memory timer.
- Everything downstream of an Incident keeps working, because a Revision is one.
- Late evidence is kept. It shows on the Call and, within the cap, in the next Revision.
- Deterministic materialization makes the closer safe to retry after a crash.
- A purge removes the raw key along with the Call.

Negative:

- Two things are named "call". `CONTEXT.md` separates Call from CaptureCall.
- Revisions lag the evidence by the grace, debounce and quiet periods. Past the cap the
  bundle stops following the evidence, which is visible but not materialized.
- Each Revision is a full bundle, so a Call with many late arrivals stores several
  near-copies.
- A Revision's `session_id` is the `call_id`, not the producer's key. Callers look a Call
  up through `POST /v1/calls:resolve`, which never echoes the key.
- A purged key is muted for LONG_QUIET + 24 h.

## Evidence

The domain terms are defined in [CONTEXT.md](../../CONTEXT.md). The call lifecycle is
implemented in `packages/sdk-python/src/earshot/live.py`,
`packages/sdk-python/src/earshot/capture/calls.py`, and
`packages/sdk-python/src/earshot/storage.py`.
