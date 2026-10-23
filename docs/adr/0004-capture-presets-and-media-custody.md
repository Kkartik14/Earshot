# ADR 0004: Store call evidence by Capture Preset and hold recordings in Earshot's blob store

Status: accepted (2026-10-19); supersedes the "Custody, not content" rule in
`docs/privacy.md`

## Context

Through 0.1.x, Earshot never stored media. The "Custody, not content" section of
`docs/privacy.md` says Earshot never ingests, fetches, caches, proxies or re-serves media
bytes. The `MediaRef` docstring says the same: a reference to media somebody else holds.
The stated reason was that holding content changes what Earshot is under consent, DSAR,
retention and breach obligations.

The product promise has changed to "see every call your voice agent makes", with audio
and transcript. A Call assembled from OTLP spans (ADR 0005) needs the transcript and LLM
text those spans carry, and a call view needs the recording. Teams that self-host own
their stack and can choose to keep that content. Regulated teams still need a mode that
keeps none of it.

Two constraints shape the decision:

- Upgrading from 0.1.x must not silently widen what an existing installation stores.
- The filter must run before anything is written. A raw batch written first and
  filtered later would already hold the content (ADR 0005).

## Decision

**Capture Preset.** Each Project has a Capture Preset, stored as
`projects.capture_preset` with a CHECK in (`full`, `metadata_only`). Operators set it
with `earshot project set-capture --project <id> --preset full|metadata_only` and read
it with `earshot project show <id>`. The server enforces it on receipt, before
persistence. It is separate from the privacy policy a producer declares inside each
Incident, and this ADR does not change the SDK's own capture default.

- **`full`** keeps content (transcripts, model and tool payloads, recordings) within
  the receiver's size limits and the recorder's attribute budget.
- **`metadata_only`** removes, before persistence:
  - content attributes: transcript, model and tool payloads, audio and identity;
  - the Session Key attributes (`earshot.session.id`, `gen_ai.conversation.id`,
    `session.id`, `conversation.id`) from the stored span and resource payloads.

  The raw Session Key is stored nowhere. `calls.session_key` is NULL, the call API
  returns `session_key: null`, and a call's transcript is `[]` with
  `transcript_coverage: "not_captured_by_preset"`. Creating a Recording returns 422
  `EARSHOT_PRESET_FORBIDS_MEDIA`.

- **`redacted`** is reserved and deferred. Asking for it returns 422
  `EARSHOT_PRESET_UNAVAILABLE`. No redaction engine exists in the codebase, and a regex
  masker in a privacy product would be a liability.

**Normalize before classifying.** The privacy classifier files several raw framework
keys as metadata, so keys are normalized first:

- Pipecat's raw `text`, `input`, `output` and `tools` become content classes;
- any `lk.pii.*` key is content;
- `user_id`, `enduser.id` and `lk.pii.participant_identity` are identity.

**Defaults.**

- Every database created by 0.2.0, on SQLite or Postgres, starts with the `default`
  Project set to `full`. Every Project created later is also `full`.
- A database opened through the legacy bootstrap (ADR 0006) sets **every** existing
  Project to `metadata_only`, in the same transaction that stamps the baseline. This
  includes a v19 file with no incidents. One log line reports the count.

**Media custody.** Earshot now holds recording bytes. A Recording's content streams
through the server into the BlobStore (ADR 0006), which computes sha256 and size while
streaming. When a Revision is built (ADR 0007), each stored Recording becomes a
`MediaRef` with:

- `integrity: content_digest`, with the sha256 and size Earshot measured;
- the content type, and a `stream_id` mapped to the speaker;
- custodian `earshot.blob_store`.

A `ClockRelation` maps the Recording's `started_at_unix_nano` to the call's clock domain.
For these references the digest is Earshot's own measurement, not only a producer's
declaration. A `MediaRef` that names another custodian keeps its old meaning. In Month 1
recordings are mono, playback streams through the server with Range support, and no
presigned URLs are exposed.

**Erasure.** Purging a Call deletes its rows and queues its blob deletions, which run
after commit with retries (ADR 0007). SQLite keeps its existing scrub. The guarantee is
weaker on the other backends:

- On Postgres the rows are gone after a purge, but WAL, backups and autovacuum delay
  physical erasure.
- On S3, deletion is a request to the store. Bucket versioning, replication and backups
  are outside Earshot's control. The production notes ask for a lifecycle rule that
  removes aborted multipart uploads.

`docs/privacy.md` is rewritten to match: the presets, the upgrade behaviour, call purge
and retention, and the weaker erasure on Postgres and S3.

## Consequences

Positive:

- A new installation shows calls with audio and transcript with no extra setup.
- An upgraded installation keeps its metadata-only posture until an operator opts a
  Project into `full`.
- `metadata_only` stays a real mode for regulated teams. It keeps timing and structure,
  and stores no raw key, no identity and no media.
- One enforcement point, before persistence, covers OTLP spans and Recordings alike.
- A stored recording's digest is checked by Earshot, not taken on trust.

Negative:

- Under `full`, Earshot holds sensitive content. The operator takes on consent, DSAR,
  retention and breach obligations for transcripts, prompts and audio. This is the cost
  the old rule avoided.
- Storage grows quickly. Voice LLM spans resend the conversation history every turn, so
  full capture grows faster than call length.
- Erasure on Postgres and S3 is weaker than on SQLite.
- There is no middle ground until `redacted` ships. A team that needs masking must
  choose `metadata_only`.
- Upgraded Projects that want audio must change the preset explicitly. Until then,
  recording uploads fail with 422.

## Evidence

The public privacy contract is described in [Privacy](../privacy.md) and
[Captured fixtures](../captured-fixtures.md). Its implementation lives in
`packages/sdk-python/src/earshot/contract.py`, `privacy.py`, and `blobstore.py`.
