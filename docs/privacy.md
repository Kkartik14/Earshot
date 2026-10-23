# Privacy and capture policy

Earshot has two privacy controls, applied at different points:

- **The Capture Preset of a Project.** The server applies it to OTLP spans and Recordings
  before it stores them. It is new in 0.2.0 and is described first.
- **The capture policy inside each Incident.** The producer declares it, and Earshot's
  SDK recorder enforces it before a value leaves the process. Its default is still
  metadata only. It is described from [Producer capture policy](#producer-capture-policy)
  on.

The two are separate. A Capture Preset does not change what the SDK records, and an
Incident's capture policy does not change what the OTLP receiver keeps.

## Capture Presets

Each Project has one Capture Preset. The server enforces it on receipt, before anything is
written, for every span sent to `/v1/traces` and every Recording. Raw OTLP requests are
never stored, so content dropped by the preset never reaches disk.

| Preset          | Spans keep                                                              | Session Key                                           | Recordings                                  |
| --------------- | ----------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------- |
| `full`          | Everything the producer sent, byte for byte, within the size limits     | Stored on the Call and returned by the call API       | Accepted and stored                         |
| `metadata_only` | Metadata attributes only, on spans, events, links, resources and scopes | Removed from stored spans and resources; never stored | Refused: 422 `EARSHOT_PRESET_FORBIDS_MEDIA` |
| `redacted`      | Not available in 0.2.0                                                  |                                                       |                                             |

Under `metadata_only`, the server removes before storage:

- **content:** transcripts, model and tool payloads, audio, diagnostic text and any
  attribute with a `pii` segment in its key;
- **identity:** `user_id`, `user.id`, `enduser.*`, `lk.pii.participant_identity`,
  `participant.name`, `user.email`, keys containing `phone_number` or `caller_id`, keys
  whose last segment is `phone` (`phone`, `user.phone`), and caller and SIP endpoints
  (`caller.number`, `callee.number`, `sip.from`, `sip.to`);
- **the Session Key attributes:** `earshot.session.id`, `gen_ai.conversation.id`,
  `session.id` and `conversation.id`;
- **GenAI message events** (`gen_ai.user.message`, `gen_ai.choice` and the like), whole;
- **span status messages**, which can quote the caller.

Keys are normalized before they are classified, because frameworks put content under
neutral names. Pipecat's raw `text`, `input`, `output` and `tools` are classified as
transcript, model input, model output and tool definitions. LiveKit's older `lk.*` content
keys map to their `lk.pii.*` names. The string `NOT_GIVEN`, which Pipecat emits for unset
values, counts as absent.

The raw Session Key is stored nowhere under `metadata_only`: a Call created under it has
a null `session_key`, and the call API returns `session_key: null` and an empty transcript
with `transcript_coverage: "not_captured_by_preset"`. Earshot still keeps
a keyed HMAC fingerprint of the key, made with the instance correlation key, so that later
spans and Recordings find the same Call. The Call ID is derived the same way.

`metadata_only` is a classifier of attribute keys, not an allowlist. It drops a key only
when the key's name says it holds content or identity, as listed above. Every other key,
and everything under it, is kept as metadata. It catches the keys of the Pipecat, LiveKit,
OpenInference and OpenTelemetry GenAI instrumentations, and the common phone and caller
keys. It does not catch:

- content under a neutral key name, for example `body`, `content`, `response`, `tts.text`
  or `llm.output`;
- values nested under a kept key. Only top-level keys are classified, so a map such as
  `agent.state = {transcript: …}` is kept whole;
- identifiers it has no name for, such as `email`, `user.name`, `call.from` or an IP
  address in `client.address`;
- secrets inside values, such as a token in the query string of `http.url` or `url.full`,
  or literals in `db.statement`.

A producer that sends such attributes must remove them itself, or not send them to a
`metadata_only` Project. Protobuf fields outside the OTLP schema are discarded. The test
suite replays a real Pipecat call under `metadata_only` and searches the stored bytes for
the raw key and identity values.

`redacted` is reserved. Asking for it is refused with `EARSHOT_PRESET_UNAVAILABLE`. There
is no redaction engine, and a regex masker would give a false sense of safety. A Project
that needs masking must use `metadata_only` until one exists.

### Defaults

- **New installs.** Every database created by 0.2.0, SQLite or Postgres, starts with the
  `default` Project set to `full`. Every Project created later is `full` too.
- **Upgrades.** A 0.1.x data directory opened by 0.2.0 sets **every existing Project to
  `metadata_only`**, in the same transaction as the upgrade, and logs
  `EARSHOT_LEGACY_CAPTURE_PRESET` with the count. This includes a data directory with no
  Incidents. An upgrade never widens what an installation stores. Until an operator opts a
  Project into `full`, its Recording uploads fail with 422.

`full` means the operator holds sensitive content: transcripts, prompts, model output,
tool arguments and audio. Consent, data-subject requests, retention and breach duties for
that content are the operator's. Voice LLM spans resend the conversation history on every
turn, so `full` storage grows faster than call length.

### Changing a preset

```bash
earshot project show support
earshot project set-capture --project support --preset metadata_only
```

A change applies to evidence received afterwards. Stored spans are not rewritten: content
received under `full` stays until its Calls are purged or its Project is deleted, because
Calls do not expire in 0.2.0 (see [retention](#calls)). A Call created under `full` keeps
its stored Session Key, and the call API still shows the transcript lines it received
before the change.

### Hosted mode

In `hosted_jwt` mode, `/v1/traces`, `/v1/recordings*`, `/v1/calls*` and `/v1/otlp/stats`
answer 403 `EARSHOT_OPERATION_UNAVAILABLE` for every method, so a hosted deployment
receives no OTLP spans, call transcripts or Recordings in 0.2.0.
Hosted Incident ingest keeps its closed metadata-only profile, which rejects media and raw
OTLP; see [the backend API](./backend-api.md).

## Producer capture policy

Earshot assumes native telemetry may be sensitive. Framework spans can already carry
transcripts, prompts, completions, tool arguments/results, error messages, caller IDs,
resource identifiers, and credential-bearing URLs.

The SDK's default policy is metadata only, with a strict safe-key allowlist. Unknown
attributes are omitted, not retained optimistically. Filtering occurs before a value
enters an exporter queue, log, protobuf payload, database, or derived analysis.

The same boundary applies to public real-capture fixtures. Their
[capture and scrubbing policy](./captured-fixtures.md) requires metadata-only source
capture, identifier pseudonymization, no raw delivery/media/content retention, a secret
scan, and acceptance by both the public CLI and `validate_incident()`. A synthetic
fixture must never be labelled as a retained real capture.

“Lossless” therefore means **lossless normalized graph and retained telemetry fields
after capture-policy filtering**. The SDK does not automatically intercept serialized
OTLP. Exact raw OTLP is a separate explicit opt-in class supplied by a caller that owns
the filtering boundary. It does not authorize unknown normalized attributes.
Every opaque OTLP chunk is intrinsically `raw_otlp`; it cannot be declared as
metadata by a caller or imported artifact. Conversely, normalized participants,
streams, operations, events, quality samples, errors, and media references cannot use
`raw_otlp` as their record class.

## Typed-metadata trust boundary

The allowlist is strict for payload-bearing attributes, nested maps, credential URLs,
quality scalars, and free-form adapter source labels. It is not a PII classifier.
Producer-controlled opaque IDs and typed operational strings—bundle/session/turn/
participant IDs, service/model names, policy IDs/versions, and syntactically valid
semantic codes—must already be pseudonymous and non-PII. Validators cannot tell
whether a lowercase identifier was semantically misused as a person's name.

Recorder entry points hash suspicious free-form status/provenance/error/source labels,
require exact `sha256:` + 64-lowercase-hex labels, and reject arbitrary string quality
values. Known metadata keys are type-specific—durations are non-negative numbers,
decisions are booleans, counters are non-negative integers, and semantic labels cannot
be arrays. JSON integers are bounded to the interoperable IEEE-754 integer domain;
decimal nanosecond strings are bounded to uint64. Native parent/link scopes are closed
and unsafe instrumentation labels are hashed. Canonical versioned
`opentelemetry.io` schema URLs are safe metadata. Other credential-free HTTPS OTel
schema URLs require `extension_payload`; without it the recorder retains only an
irreversible digest. Imports enforce the same structural and known-label rules.
Applications remain responsible for the producer-owned identifier namespace.

## Capture classes

```text
metadata
extension_payload
transcript
audio
tool_payload
model_payload
diagnostic_payload
identity
raw_otlp
```

`extension_payload` is the explicit forward-compatibility grant for unknown profile or
record fields. It remains separate from `raw_otlp`, so retaining an opaque OTLP chunk
cannot silently turn metadata records into free-form payload containers. Extension
keys still use the bounded semantic-key grammar. This authorization is independent of
a record's primary class: a transcript/model record does not gain an undeclared custom
field channel merely because its main payload class is enabled.

Each class records the requested decision and actual capture result. Enabling a
sensitive class should include consent/legal basis where applicable, retention,
redaction policy/version/results, and export restrictions. `CapturePolicy.governance`
can carry all four records and the recorder writes them into the immutable manifest.

## Omission ledger

Every filtered field contributes a non-sensitive omission record with a SHA-256 of
the field key, capture class, reason, and optional count/digest. Neither the key nor
the value needs to survive. The ledger proves that evidence was deliberately omitted
without retaining its content.

Validation and API errors return stable codes and paths; they do not echo offending
values. Application logging must follow the same rule.

## Media

Since 0.2.0, Earshot holds audio for Projects whose preset is `full`. Before 0.2.0 it
never stored media bytes; that rule is superseded by
[ADR 0004](./adr/0004-capture-presets-and-media-custody.md).

### Recordings held by Earshot

A Recording's bytes stream through the server into the blob store, local disk or S3,
under a server-generated key. Earshot computes the sha256 and size while streaming.
Client strings never appear in blob keys. Playback streams through the server with
`Range` support; there are no presigned URLs. Recordings are mono in 0.2.0.

When a Call closes, each stored Recording becomes a `MediaRef` in the Revision, with
custodian `earshot.blob_store` and `integrity: content_digest`. For these references the
digest is Earshot's own measurement, not only a producer's declaration. A `ClockRelation`
maps the Recording's start time to the Call's clock domain.

### Media held by others

A `MediaRef` naming any other custodian keeps its earlier meaning: custody metadata for
media somebody else holds. Earshot never fetches, caches, proxies, or re-serves those
bytes.

- Audio bytes are never inline in the profile.
- Metadata-only bundles contain no audio reference or locator.
- Locators are separately governed. Credential-shaped locators are removed by the
  recorder and rejected on untrusted import.
- Credential-bearing locators are invalid.
- Validators and analyzers never fetch submitted URLs (an SSRF boundary).

Because Earshot never reads those bytes, it cannot honestly assert a digest over them, so
`MediaRef.integrity` says which case a reference is in:

- `content_digest` — somebody measured the bytes and declared `sha256` and `size_bytes`.
  For another custodian's media, that digest is a declaration carried by the artifact,
  not an Earshot verification.
- `opaque_handle` — nobody on this path measured them. The reference carries no digest
  and no size, names the `custodian` who holds them, and cannot declare a `byte_range`
  into a length nobody observed.

Either shape may also declare `consent`, `retention`, a covered `time_range`, and the
media's own `clock_domain_id`. Alignment to the incident timeline is an ordinary
`ClockRelation` between that domain and the session's — there is no separate media
synchronization model. Media no declared calibration reaches stays honestly unaligned
(`EARSHOT_MEDIA_UNALIGNED`, a warning): unalignable custody is still legitimate custody,
it simply cannot be overlaid.

The viewer never emits a media `src` for another custodian's media, which would fetch it
on render. A declared locator is offered only as a user-initiated link that goes directly
from the reader's browser to the custodian, with the reader's own credentials and no
referrer.

## Restricted export

A restricted export must reapply its destination policy. Artifact retrieval, incident
listing, cached analysis, and CLI output all enforce their named destination. The
catalog projection filters denied incidents before pagination, so a cursor cannot
encode a restricted bundle ID.

The live SSE tail is one of these egress paths, not an exemption from them, and it
enforces its own destination name `live_tail` rather than borrowing one written for the
reviewed one-shot reads. It has no finished bundle to check, so it holds the two halves
that answer the same question mid-session: the capture policy the journal header declares,
and the classes the journal has actually retained so far. The keying is identical to a
bundle's — a class restricts egress once it has been _captured_, not merely enabled — and
the destination rule itself is the single function every seam calls. Because the recorder
journals a newly retained class in the same frame as the mutation that retained it, the
frame that first carries restricted content is the frame that closes the gate; nothing
restricted precedes it. From there the record's content does not leave the process. Its
existence still does: a `withheld` event occupies that journal slot and names the
destination and the classes that refused, so a live view can never read as a complete
account of a session it was only partly permitted to show. The check is fail-closed — an
unreadable policy or an unresolvable capture class withholds rather than passes.

If source evidence is removed, any derived result depending on it must be removed or
recalculated against a new digest. An analysis keyed to one input digest is never
silently attached to a different redacted artifact.

DerivedAnalysis is a closed metadata-only sidecar. It cannot add extension payloads;
its evidence references, units, measurement names, clock domains, digest, session,
version, generated time, and summary counts are revalidated before storage and return.

## Retention and erasure

### Incidents

Immutable means no in-place evidence mutation. It does not override deletion or
retention obligations. Purge physically removes the artifact and its derived analysis,
then writes a content-free tombstone preventing accidental ID reuse. Tombstones contain
only `SHA-256(bundle_id)` and the purge-operation timestamp. They contain no plaintext
bundle/session ID, incident/session timing, content digest, participant, or payload
metadata.

The store indexes the earliest absolute expiry or creation-time-plus-TTL across all
captured classes. Because the artifact is immutable, the most restrictive retained
class expires the whole bundle. Expired bundles are rejected at every direct read and
excluded from incident, Turn Fact, and summary queries. The API lifespan runs a
bounded reaper that removes expired rows and artifacts without making a listing wait for
the full expiry backlog. Its interval and batch size can be set with
`EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS` and
`EARSHOT_RETENTION_CLEANUP_BATCH_SIZE`.

### Calls

A Call's spans, Recordings and Revisions are purged together.

```bash
curl -X DELETE http://127.0.0.1:4319/v1/calls/<call_id>
earshot call purge --project <project_id> <call_id>
```

The route answers 204, or 404 for an unknown Call; the command exits 0, or 2 with
`EARSHOT_CALL_NOT_FOUND`. `--project` defaults to `default`. In one transaction, a purge:

- deletes the Call, its spans, its trace bindings, its Recording rows and its Revision
  rows, so no raw Session Key survives and the Call is no longer listed;
- purges each Revision through the Incident purge above, leaving Incident tombstones;
- queues the Recording blobs for deletion, which runs after the commit and is retried
  until it succeeds;
- removes resource rows no other span of the Project uses;
- increments the Session Key's generation and writes a Call tombstone for the key and one
  for each of the Call's traces.

**Key generations and tombstones.** The generation row and the Call tombstones hold only
the Project ID, a keyed HMAC fingerprint (of the Session Key, or for a trace tombstone of
the trace ID), a generation number and, for a tombstone, an expiry time. A tombstone
lives for `EARSHOT_CALL_LONG_QUIET_SECONDS` plus 24 hours. While it lives, new spans and
Recordings for the key, and late spans of the purged Call's traces, keyed or not, are
dropped and counted as `purged_call_evidence_dropped`, so a retrying exporter cannot
rebuild the Call. A new Recording for the key is refused with 409 `EARSHOT_CALL_PURGED`.
After it expires, evidence for the same key starts a new Call with a new ID, because the
generation changed. A purged Call ID and its Revision bundle IDs are never reused. The
server deletes expired Call tombstones within a minute or so of their expiry. The
generation row stays until the Project is deleted.

A Revision cannot be purged on its own: `DELETE /v1/incidents/<bundle_id>` on a Revision
answers 409 `EARSHOT_INCIDENT_BELONGS_TO_CALL` with the `call_id`, and
`earshot purge <bundle_id>` exits 2 and names the `earshot call purge` command to run.
Purge the Call.

**Retention.** In 0.2.0 no Call expires. Revisions carry no retention policy, so a Call
and all its evidence are kept until it is purged or its Project is deleted. The rules
below apply once Revisions carry one. The Incident reaper skips Revisions, so earlier
Revisions never expire on their own, not even when one is read. When a Call's latest
Revision expires, the server purges the whole Call as above, in bounded batches. An open
Call has no Revision, so it never expires.

**Project deletion** (`DELETE /v1/projects/<project_id>`, hosted JWT mode only; see
[the backend API](./backend-api.md#project-deletion)) removes every span, parked trace,
Recording, blob, resource, Revision, Call tombstone, key generation and OTLP counter of
the Project. Incident tombstones (`SHA-256(bundle_id)`), including those of Revisions,
stay, so bundle IDs are never reused. Blobs are deleted after the catalog commit; deletion
answers 202 until they are gone.

### Erasure limits

**SQLite** uses `secure_delete=ON`, full synchronous writes, WAL checkpoint/truncation,
file/directory fsync, and CAS unlinking. The reaper compacts SQLite at most once every 10
minutes after its expiry backlog drains, outside the cross-process mutation lock; an
explicit purge compacts at once. A separate compaction lock coalesces scrub work across
store workers, and a durable scrub generation causes a later pass to retry if compaction
fails. A Call purge also compacts at once. A Call purged because it expired is scrubbed
as the reaper scrubs: compaction waits until the expired backlog drains and runs at most
once every 10 minutes.

**Postgres** deletes the rows, artifact bytes included, in the purge transaction. There is
no secure delete and no scrub, so physical erasure is delayed:

- dead row versions stay in heap and index pages until autovacuum or page reuse
  overwrites them;
- copies stay in the WAL, including archived WAL used for point-in-time recovery, until it
  is recycled or expires;
- replicas, base backups and `pg_dump` files keep their copies until they age out.

**Blob stores.** On local disk a deleted Recording is unlinked. On S3, a deletion is a
request to the store: with bucket versioning on, the object survives as a noncurrent
version; replication and bucket backups keep their copies. Earshot controls none of these.
See [the S3 notes](./production.md#s3-lifecycle-rule).

All of this is best-effort, file-level erasure, not a physical-media guarantee:
copy-on-write filesystems, SSD wear leveling, snapshots, and backups may retain old
blocks. Deployments requiring a stronger claim must encrypt artifacts and recordings with
disposable per-tenant or per-retention-domain keys and govern snapshots/backups
separately.

### Checkpoint journals

The SDK's optional checkpoint directory (`checkpoint_dir` / `EARSHOT_CHECKPOINT_DIR`) is
a second at-rest surface with the same discipline as the durable spool: off by default,
an explicit owner-private `0700` directory holding `0600` files, symlinks refused, and
optional AES-256-GCM envelope encryption with the same key precedence and the same
crypto-shredding semantics (an encrypted journal with no key is unreadable, never
half-decoded). A journal only ever holds capture classes the configured policy already
admits, so enabling checkpointing never widens what Earshot retains, and it holds no
separate content channel of its own. A journal is removed once its incident reaches a
durable successor; anything left behind by a crash is a recoverable artifact and is
subject to the same erasure obligations as the spool.

## Conformance secret sentinel

Tests inject unique sentinel secrets into governed sensitive source categories and
adversarial free-form adapter fields, then verify
they do not occur in:

- debug JSON or protobuf bytes;
- raw OTLP chunks;
- SQLite/index files;
- content-addressed filenames;
- checkpoint journal (`.eck`) bytes and filenames;
- analysis output;
- API/CLI errors; or
- logs and exporter diagnostics.

Validation paths replace attacker-controlled attribute keys with `<key>`, and error
messages are stable templates. HTTP exporters refuse non-HTTPS remote endpoints and
reject redirects, preventing bearer credentials from being forwarded cross-origin.

Metadata-only conformance fails if any governed sentinel survives. This guarantee does
not extend to producer-controlled IDs or semantic codes that violate the trust rule
above while still matching their declared syntax. Earshot never logs a database password
or an S3 secret; a test greps the output of both server entry points for them.
