# ADR 0005: Receive OTLP at the root `/v1/traces` and group spans into Calls

Status: accepted (2026-10-19); supersedes
[ADR 0003](./0003-finalized-connectors-before-live-otlp.md)

## Context

ADR 0003 held back a generic OTLP receiver until Earshot had specified project routing,
deduplication, completion and idle timeout, late spans, crash recovery, bounded staging
and privacy. Month 1 makes OpenTelemetry the main way in, and this ADR records those
policies.

Facts that constrain the design:

- The Python OTLP HTTP exporter appends `v1/traces` to `OTEL_EXPORTER_OTLP_ENDPOINT` and
  sends protobuf. It retries 408 and 5xx but not 429, ignores `Retry-After`, and never
  reads `partial_success`.
- The OTLP spec expects duplicates, because clients re-send when unsure.
- Frameworks key sessions differently. Pipecat puts `conversation.id` only on its
  `conversation` and `turn` spans; its `stt`, `llm` and `tts` spans share the trace but
  carry no key. LiveKit 1.8 sets `gen_ai.conversation.id` to the room SID.
- Neither Langfuse nor Phoenix ever closes a trace or session. Closing a call on an end
  signal or a quiet period is new.

## Decision

**Endpoint.** `POST /v1/traces` at the root, so `OTEL_EXPORTER_OTLP_ENDPOINT` alone
works. It is OTLP/HTTP only. gRPC is out of Month 1, and the docs point Pipecat at the
HTTP exporter.

- It accepts `application/x-protobuf` and `application/json`, chosen by parsed media
  type, and `Content-Encoding` `identity`, `gzip` or `deflate`; anything else is 415.
  Decompressed bodies are bounded at 32 MiB (protobuf) and 4 MiB (JSON); above is 413.
- JSON hex IDs, link IDs included, are converted before `ParseDict`, so both encodings
  decode to the same bytes.
- A span with invalid IDs, or times outside [1, 2^63−1], or an end before its start, is
  rejected alone through `partial_success`. Valid spans in the same request are stored.
- Auth is the existing middleware. A hosted JWT gets the existing 403
  `EARSHOT_OPERATION_UNAVAILABLE` on `/v1/traces`, `/v1/recordings*`, `/v1/calls*` and
  `/v1/otlp/stats`. Under `trust_local_network`, a valid bearer API key is exempt from
  the loopback-Host check.

**Session Key.** Span attributes are checked in this order: `earshot.session.id` ›
`gen_ai.conversation.id` › `session.id` › `conversation.id`. On the resource, only
`earshot.session.id` is read. A key must be UTF-8 of 1–256 bytes after trimming; an
invalid key counts as no key. The Call records the winning attribute as `key_source`.

`session.id` sits below `gen_ai.conversation.id` because of what OTel means by a session:
"the period of time encompassing all activities performed by the application", ending on
user inactivity or timeout. One such session can hold several calls. A browser voice
widget whose OTel session processor stamps `session.id` on every span would merge two
calls in one tab into one Call, and because trace binding is first-wins, the merge could
not be undone. `gen_ai.conversation.id` names one conversation; LiveKit sets it to the
room SID, which is one call. An app's own key belongs in `earshot.session.id`, which wins
under any order and is the key the Recordings API takes.

**Binding and parked traces.**

- A trace binds to at most one Call, and the first binding wins. Unkeyed spans of a
  bound trace (Pipecat `stt`, `llm`, `tts`) join that Call. Key disagreements within a
  trace are counted.
- Spans of an unbound trace are parked. A keyed span in any later request binds the
  trace, and its parked spans join the Call.
- A parked trace's deadline is its last received span plus LONG_QUIET. At the deadline, a
  trace with a recognized framework session span becomes a trace-keyed Call
  (`key_source='trace_id'`). Otherwise its spans are deleted and counted.

**End markers** are matched by instrumentation scope and span name, never by root span
status: Pipecat scope `pipecat.turn` span `conversation`; LiveKit span `agent_session`
(≤1.7) and `session_close` (1.8); and any span with `earshot.call.ended=true`.

**Close timing.** `close_due` is recomputed on every evidence write:

- after an end marker: now + END_GRACE (10 s);
- otherwise: now + LONG_QUIET (300 s);
- for a closed Call receiving late evidence: the later of now + LATE_DEBOUNCE (30 s) and
  the applicable rule above. It is never computed from the stored `close_due`.

A Call with no spans, created by a Recording, is not due until it has an end marker or
LONG_QUIET has passed. Separately, a Call with any `pending` Recording created less than
UPLOAD_HOLD_CAP (600 s) ago cannot be claimed. Each duration has an `EARSHOT_CALL_*` or
`EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` setting. Late evidence produces a new Revision
(ADR 0007).

**Persistence.** The receiver persists no raw OTLP batches: content would land before the
Capture Preset applied, and a per-call purge would be impossible. It applies the
Project's preset (ADR 0004), serializes each span deterministically, and writes the
filtered span and resource rows. One request is one `_mutation()` transaction, and the 200
is sent only after commit.

**Duplicates.** Spans are keyed by `(project, trace_id, span_id)`. An identical re-send
creates no rows and no Revision. A different payload for an existing span keeps the first
write and increments `span_conflicts`.

**Responses.**

- 200 with `ExportTraceServiceResponse` in the request's encoding, with
  `partial_success` when spans were rejected. An empty request gets 200 too.
- 400 for an undecodable body, 413 for oversize, 415 for an unsupported type or encoding.
- 503 with `Retry-After: 3` when the admission limiter (sized to the database pool) is
  full, when the write path cannot get its locks within a 2 s total budget, or when a
  pool checkout times out.
- **Never 429.** The Python exporter does not retry it, so the batch would be lost.
- Handler errors are `google.rpc.Status` bodies in the request's encoding. Errors from the
  shared auth and host middleware (401, 403, host checks) keep Earshot's problem JSON,
  because those checks run before the route is known.

`GET /v1/otlp/stats` returns the per-project rare-event counters (rejected spans, span
conflicts, discarded parked traces, and so on) and gauges. Nothing is written per span.

**Semantic conventions.** `semconv/earshot.yaml` registers `earshot.session.id` and
`earshot.call.ended`. This reverses the file's earlier note, which reused OTel
`session.id` "rather than duplicated as `earshot.session.id`". `session.id` has the wrong
scope for a call, and Earshot's own OTLP exporter already stamps `earshot.session.id`.

## Consequences

Positive:

- A Pipecat app needs one environment variable, plus an API-key header off loopback.
- Retries are safe: duplicates are no-ops and acknowledged spans are durable. The
  Capture Preset applies before anything reaches disk.
- Overload uses a status the real Python client retries, and unkeyed noise does not
  flood the call list.

Negative:

- A call that never sends an end marker (a crashed process, a missed flush) closes only
  after 5 minutes, as `interrupted`.
- A producer cannot correct a span by re-sending it; the conflict is only counted.
- Python clients never see per-span rejections, and unkeyed traces from unrecognized
  frameworks are discarded after LONG_QUIET. Operators must read `/v1/otlp/stats`.
- A producer that keys several calls only by one `session.id` still gets them merged.
  The fix is to set `earshot.session.id`.
- Pipecat's own tracing example uses the gRPC exporter and must be switched to HTTP.
- Middleware errors are problem JSON, not `google.rpc.Status`. A Collector logs them;
  revisit if a Collector-specific failure is observed.

## Evidence

The receiver contract is documented in [Backend API](../backend-api.md) and
[OTLP quick start](../otlp-quickstart.md). The implementation and public semantic
definitions are in `packages/sdk-python/src/earshot/` and `semconv/earshot.yaml`.
The external protocol references are [OTLP](https://opentelemetry.io/docs/specs/otlp/),
[OpenTelemetry session semantics](https://opentelemetry.io/docs/specs/semconv/general/session/),
and the [protobuf JSON mapping](https://protobuf.dev/programming-guides/json/).
