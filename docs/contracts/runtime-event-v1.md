# Proposed TVIC runtime event contract v1

**Status:** Earshot proposal; pending acceptance from Platform and TVIC owners  
**Owner:** Earshot owns the receiving evidence model and privacy rules  
**Transport:** Platform host-side worker to Earshot; no TVIC-to-Earshot direct call

This proposal defines the minimum metadata a host may map from a TVIC runtime
session into Earshot. It is not yet a cross-service accepted schema, and there
is no TVIC producer in this repository. Do not claim a staged call has passed
until Platform and TVIC agree on field names, event timing, retry ownership,
capture policy, and conformance evidence.

## Envelope

The host-side worker forwards versioned events using a durable at-least-once
outbox. The wire envelope is independent of the `/v1` HTTP API version:

```json
{
  "schema_version": "tvic.earshot.runtime-event.v1",
  "project_id": "00000000-0000-0000-0000-000000000001",
  "call_id": "call_01J...",
  "runtime_session_id": "session_01J...",
  "event_id": "event_01J...",
  "event_type": "runtime.call.started",
  "runtime": { "name": "voice-runtime", "version": "1.2.0" },
  "time": {
    "source_time_unix_nano": "1780000000000000000",
    "observed_time_unix_nano": "1780000000001000000"
  },
  "attributes": { "outcome": "started" }
}
```

This example is illustrative. Exact event types and the attribute allowlist
need runtime-owner review before implementation. The schema must reject unknown
versions, unbounded maps, non-finite numbers, and values outside per-field size
limits.

| Field                | Owner and rule                                                                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`     | Required exact version string; a breaking change gets a new major version.                                                                                                          |
| `project_id`         | Canonical Platform project UUID, for correlation only. The host's short-lived Earshot JWT is the authority and its signed claim must match.                                         |
| `call_id`            | Stable Platform product call ID; unchanged through retries and status recovery.                                                                                                     |
| `runtime_session_id` | Optional TVIC session ID when the public runtime surface exposes one. It is correlation metadata; the Platform host allocates and persists Earshot `session_id` at durable enqueue. |
| `event_id`           | Stable event ID allocated and persisted by the Platform host at durable enqueue; reused for every retry and never derived from a timestamp.                                         |
| `event_type`         | A bounded, versioned enum agreed by TVIC and Earshot; no arbitrary event names from provider payloads.                                                                              |
| `runtime`            | Lowercase bounded runtime-family slug and semantic release version; no provider/model names, environment, host, or deployment secrets. The host maps only runtime identity.         |
| `time`               | Preserve source wall time when supplied and record host observation time separately. Do not invent source time or include monotonic readings in this version.                       |
| `attributes`         | Closed event-specific allowlist of categorical or numeric metadata only. No free-form text or arbitrary nested payloads.                                                            |

The initial event set should be limited to lifecycle and runtime-health
metadata: call/session started, terminal outcome, and approved operation
boundaries. The accepted lifecycle vocabulary must represent successful,
failed, and cancelled terminal outcomes. Its exact enum and literals remain
pending agreement with TVIC and Platform. Whether operation names,
provider/model labels, and detailed failure codes are included requires an
explicit field-level decision. Earshot must not assume the example strings
above are already produced by TVIC.

The hosted manifest accepts only a lowercase runtime-family slug and a numeric
`MAJOR.MINOR.PATCH` release version. The Platform worker must map those fields from runtime identity;
provider and model labels remain excluded unless the owners approve them as
separate fields.

TVIC's current public event surface does not provide a stable event ID, source
timestamp, or stable runtime-session lifecycle identity on each event. The host
therefore owns durable event and Earshot session identity. Source timestamps and
runtime session IDs remain optional unless TVIC adds them to an accepted public
contract.

## Mapping into Earshot

The Platform worker, not the public TVIC SDK, owns the mapping:

- The Platform host allocates and persists the incident profile's `session_id` at
  durable enqueue. An available `runtime_session_id` is retained only as approved
  correlation metadata.
- The host outbox's `event_id`, `event_type`, and available source/observed time become a normalized Earshot
  `Event` identity, name, and `TimePoint`.
- `call_id` is stored as the mutable catalog reference
  `(namespace=platform, record_type=call)`, not copied into event content.
- `project_id` is checked against the signed project claim and is not accepted
  as authorization from the JSON body.
- Runtime identity and the event-specific metadata allowlist are mapped to the
  corresponding normalized Incident fields. Arbitrary attributes are not
  forwarded.

The Platform worker assigns and durably records one opaque artifact-submission
key for each immutable evidence snapshot. Before Earshot acknowledges the first
submission, the outbox retains the event IDs, that key as `Idempotency-Key`, and
the exact serialized body bytes; it does not yet know the Earshot bundle ID. The
request omits `profile.manifest.bundle_id`. Earshot derives and assigns the opaque
bundle ID from its private durable correlation key and the signed project scope,
then returns it for the Platform worker to persist in its call/session mapping.
A retry with the same key and body returns `200` with the same bundle ID; first
ingest returns `201`. Reusing a key with changed content returns `409` and must
be quarantined for reconciliation, not retried with new bytes. After acknowledgement,
the host retains the returned bundle ID alongside the submission mapping. A
legitimately later snapshot gets a new submission key and Earshot bundle ID under
the same session ID; it does not update the prior artifact. The exact relationship
between event IDs and artifact-submission keys remains subject to the cross-service
producer contract.

For a live stream, the host may submit Earshot checkpoint frames with stable
session identity and monotonically increasing frame sequence. It retries the
same frame bytes. `409 EARSHOT_CHECKPOINT_SEQUENCE_GAP` requires sequence
reconciliation; `409 EARSHOT_CHECKPOINT_DIVERGED` indicates the retry body
changed and must stop for investigation. Live journals are not immutable
incidents; only an accepted final bundle or an explicitly labeled recovery
operation creates one.

## Retry and failure behavior

The Platform host outbox owns retry state and retains event IDs, the submission
key, and exact body bytes until it records an Earshot acknowledgement. Delivery
is at least once. Retries reuse those same IDs, key, and bytes; after acknowledgement,
the host also persists Earshot's returned bundle ID. The host honors
`Retry-After` for `429` and `503`, applies bounded exponential backoff for
transient network errors, and stops/alerts on `401`, `403`, schema validation,
or content-divergence errors. It must not report a call as fully observed while
its evidence delivery is pending or rejected. Earshot's project deletion fence
rejects pending writes; Platform's project-deletion coordinator cancels its
outbox and waits for the Earshot deletion acknowledgement.

## Content exclusions

The default event contract is metadata-only. It explicitly excludes:

- raw or partial audio, audio URLs, recording locators, and audio-derived text;
- transcripts, prompts, model completions, and dynamic variables;
- tool/function names unless separately allowlisted, arguments, results, and
  tool payloads;
- provider webhook bodies, SDK debug dumps, arbitrary event/resource maps, and
  raw OTLP attributes;
- phone numbers, email addresses, user names, credentials, API keys, cookies,
  authorization headers, and environment/deployment details; and
- unbounded exception messages, stack traces, URLs, and free-form text.

Adding any excluded class requires an explicit capture policy that names its
purpose, allowed fields, retention period, access controls, and user-visible
indication. It also requires new approval from the service owners; this document
does not grant that approval.

## Acceptance evidence required

Before marking this contract accepted, Platform and TVIC owners must agree on
the exact envelope schema, event enum, event-to-Incident mapping, and clock
semantics. Then run a staged two-project conformance flow that proves stable
IDs and timestamps, byte-identical retries, duplicate delivery, changed-body
conflict handling, metadata exclusions, project isolation, pending-delivery UI
state, and deletion while an outbox retry is pending. Earshot must publish its
OpenAPI schema and report the resulting staged session ID only after that flow
passes.
