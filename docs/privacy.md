# Privacy and capture policy

## Default: metadata only

Earshot assumes native telemetry may be sensitive. Framework spans can already carry
transcripts, prompts, completions, tool arguments/results, error messages, caller IDs,
resource identifiers, and credential-bearing URLs.

The default policy uses a strict safe-key allowlist. Unknown attributes are omitted,
not retained optimistically. Filtering occurs before a value enters an exporter queue,
log, protobuf payload, database, or derived analysis.

The same boundary applies to public real-capture fixtures. Their
[capture and scrubbing policy](./captured-fixtures.md) requires metadata-only source
capture, identifier pseudonymization, no raw delivery/media/content retention, a secret
scan, and acceptance by both the public CLI and `validate_incident()`. A synthetic
fixture must never be labelled as a retained real capture.

“Lossless” therefore means **lossless normalized graph and retained telemetry fields
after capture-policy filtering**. M1 does not automatically intercept serialized OTLP.
Exact raw OTLP is a separate explicit opt-in class supplied by a caller that owns the
filtering boundary. It does not authorize unknown normalized attributes.
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

- Audio bytes are never inline in the profile.
- Metadata-only bundles contain no audio reference or locator.
- Locators are separately governed. Credential-shaped locators are removed by the
  recorder and rejected on untrusted import.
- Credential-bearing locators are invalid.
- Validators and analyzers never fetch submitted URLs (an SSRF boundary).

### Custody, not content

A media reference is custody metadata for media somebody else holds. Earshot never
ingests, fetches, caches, proxies, or re-serves the bytes — not even encrypted, because
encrypted content is still content and storing it would change what earshot is under
consent, DSAR, retention, and breach obligations.

Because earshot never reads the bytes, it cannot honestly assert a digest over them, so
`MediaRef.integrity` says which case a reference is in:

- `content_digest` — somebody measured the bytes and declared `sha256` and `size_bytes`.
  That digest is a declaration carried by the artifact, not an earshot verification.
- `opaque_handle` — nobody on this path measured them. The reference carries no digest
  and no size, names the `custodian` who holds them, and cannot declare a `byte_range`
  into a length nobody observed.

Either shape may also declare `consent`, `retention`, a covered `time_range`, and the
media's own `clock_domain_id`. Alignment to the incident timeline is an ordinary
`ClockRelation` between that domain and the session's — there is no separate media
synchronization model. Media no declared calibration reaches stays honestly unaligned
(`EARSHOT_MEDIA_UNALIGNED`, a warning): unalignable custody is still legitimate custody,
it simply cannot be overlaid.

The viewer renders these facts and nothing else. It never emits a media `src`, which
would fetch the media on render; a declared locator is offered only as a user-initiated
link that goes directly from the reader's browser to the custodian, with the reader's own
credentials and no referrer. No byte of media passes through earshot.

## Restricted export

A restricted export must reapply its destination policy. Artifact retrieval, incident
listing, cached analysis, and CLI output all enforce their named destination. The
SQLite projection filters denied incidents before pagination, so a cursor cannot
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

Immutable means no in-place evidence mutation. It does not override deletion or
retention obligations. Purge physically removes the artifact and its derived analysis,
then writes a content-free tombstone preventing accidental ID reuse. Tombstones contain
only `SHA-256(bundle_id)` and the purge-operation timestamp. They contain no plaintext
bundle/session ID, incident/session timing, content digest, participant, or payload
metadata.

The store indexes the earliest absolute expiry or creation-time-plus-TTL across all
captured classes. Because the artifact is immutable, the most restrictive retained
class expires the whole bundle. Expiry is enforced on startup and before reads,
analysis access, and listings—not merely by an optional maintenance job.

SQLite uses `secure_delete=ON`, full synchronous writes, WAL checkpoint/truncation,
`VACUUM`, file/directory fsync, and CAS unlinking. This is best-effort file-level
erasure, not a physical-media guarantee: copy-on-write filesystems, SSD wear leveling,
snapshots, and backups may retain old blocks. Deployments requiring a stronger claim
must encrypt artifacts with disposable per-tenant or per-retention-domain keys and
govern snapshots/backups separately.

The SDK's optional checkpoint directory (`checkpoint_dir` / `EARSHOT_CHECKPOINT_DIR`) is
a second at-rest surface with the same discipline as the durable spool: off by default,
an explicit owner-private `0700` directory holding `0600` files, symlinks refused, and
optional AES-256-GCM envelope encryption with the same key precedence and the same
crypto-shredding semantics (an encrypted journal with no key is unreadable, never
half-decoded). A journal only ever holds capture classes the configured policy already
admits, so enabling checkpointing never widens what earshot retains, and it holds no
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
above while still matching their declared syntax.
