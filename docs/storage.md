# Storage, graph indexing, retention, and purge

The local backend uses two coordinated stores:

```text
canonical evidence: objects/sha256/<2 hex>/<62 hex>
derived/index state: earshot.sqlite3
```

The canonical deterministic protobuf is the source of truth for evidence content and
graph facts. SQLite is the durable authority for publication, bundle identity,
ingest order, retention/export decisions, and purge tombstones. The two stores form
one persistence unit together with `instance-correlation.key` and must be backed up and
restored together; CAS bytes alone cannot reconstruct deleted-ID tombstones or original
ingest ordering, while a missing correlation key changes webhook Receipt and External
Identity fingerprints and the deterministic IDs Earshot mints for hosted artifacts.

## Ingest publication order

All filesystem and database mutations are serialized by a process `RLock` and, on
Unix, an advisory file lock shared by backend processes using the same directory.

An ingest does the following inside that mutation boundary:

1. Validate the structural, semantic, graph, privacy, and hash contract.
2. Re-encode and compare any caller-supplied canonical payload.
3. Begin an immediate SQLite transaction, reject tombstone reuse or a same-ID/different-
   digest conflict, then commit a project-scoped ingest intent containing the CAS digest.
4. Write a temporary object, flush and fsync it, then atomically hard-link it to its
   SHA-256 path and fsync the containing directories.
5. Begin an immediate SQLite transaction and insert the project-scoped incident row,
   graph and Turn Fact projections, earliest expiry, and destination export decisions.
6. Remove the ingest intent in the same transaction and commit before returning
   `created=true`.

The CAS object remains inside the same cross-process critical section until the index
commit. If the process stops after publishing the object, the committed intent still
proves which project owns the orphan; startup recovery or project deletion removes it
when no incident references the digest. Truly unattributed orphans remain preserved.

## Relational projections

Schema version 18 indexes:

- `projects` and `api_keys`: authorization scope, active/deleting/deleted lifecycle,
  and memory-hard credential hashes;
- `incidents`: project, identity, digest, status, finality/completeness, framework,
  creation/ingest times, earliest expiry, and destination export decisions;
- `incident_ingest_order`: monotonic ingest sequence for finite startup expiry sweeps,
  removed with its incident;
- `operations`: normalized OTel identity, parentage, participant/stream/turn, source
  and monotonic boundaries, evidence summary, and capture class;
- `causal_links`: ordered typed edges from an operation to internal/external targets;
- `events`: point identity, operation/trace correlation, participant/stream/turn,
  timestamps, evidence summary, and capture class;
- `turn_metrics`: rebuildable wide Turn Facts with per-measurement availability, basis,
  confidence, limitation, response-model and STT-language fleet dimensions, and a dedicated
  projection version. Common fields include STT finalization, EOU, first token/audio,
  transport/render response, explicit native turn duration, tools, and evidence-qualified
  accepted-interruption count;
- `connectors`, `delivery_receipts`, and `external_identities`: provider trust
  configuration, replay/content-digest state, and instance-keyed HMAC correlation;
- `analyses`: analyzer version + exact input digest + strict JSON output; full uint64
  generation times are canonical decimal `TEXT`, not signed SQLite integers;
- `incident_external_references`: mutable Platform/Voice Labs record IDs, project and
  incident scoped with a composite foreign key so purge/retention removes the links;
- `tombstones`: only `SHA-256(bundle_id)` and the purge-operation time; and
- `pending_object_deletions`: project-attributed CAS digests whose incident rows
  committed as deleted but whose files still require unlink; and
- `pending_ingests`: project-attributed CAS publications not yet committed to an incident
  index row; successful ingest removes the intent in the same catalog commit; and
- `legacy_deletion_reviews`: pre-v13 projects already marked deleting whose earlier
  cleanup did not record per-artifact CAS ownership.

Foreign keys cascade graph and analysis rows when an incident is deleted. Graph rows
and Turn Facts are derived and rebuilt on startup, which also backfills retention/export
projections when an older database is migrated. Existing stores migrate into the
`default` Project. A legacy session index is explicitly rebuilt with `project_id` so
schema upgrades do not silently retain a cross-project query plan.

## Idempotency and concurrency

The tuple `(bundle_id, canonical_digest)` defines an exact retry. The same pair returns
the existing row. Reusing a bundle ID with different content is a conflict. A purged
bundle ID can never be reused.

Provider Deliveries use a separate durable Receipt keyed by Connector plus an HMAC of
the provider delivery identity. Same identity and same body digest is a replay; the same
identity with a different body is a conflict. A processing lease makes concurrent
duplicates retryable without publishing twice. Completion/failure compares the current
attempt token, so a worker whose lease expired cannot overwrite its successor. Raw
provider bodies are not retained.

Reads verify CAS bytes against the indexed digest. Artifact read and concurrent purge
share the mutation boundary, so a successful read cannot race an unlink into a false
success. Missing or mismatched bytes are corruption, not a not-found response.

## Retention

Each captured class may declare an absolute `expires_at_unix_nano`, a `ttl_nano`
relative to immutable bundle creation, or both. Earshot stores the earliest deadline
across all captured classes. Selective in-place deletion would change the artifact and
digest, so the strictest class expires the whole bundle.

`purge_expired(now, limit)` is available for explicit maintenance. Enforcement is also
automatic in the running API:

- during store startup;
- before a direct record, artifact, or analysis read; and
- in a lifespan-managed reaper that deletes at most one configured batch at a time.

Incident and Turn Fact listings and aggregates exclude expired rows in their catalog
queries, so expired metadata is never returned while the reaper is between batches.
Those reads do not start physical cleanup themselves. The reaper releases the store lock
between batches. Each batch deletes at most the configured number of catalog rows and
syncs only the affected CAS shards. Intermediate batches checkpoint and truncate the
WAL; the batch that drains the current backlog performs one `VACUUM` after releasing the
store lock. SQLite still arbitrates access to the database file during compaction. A
durable scrub generation keeps failed compaction retryable without vacuuming on every
idle pass. The interval and batch size are configurable with
`EARSHOT_RETENTION_CLEANUP_INTERVAL_SECONDS` (default `5`) and
`EARSHOT_RETENTION_CLEANUP_BATCH_SIZE` (default `1000`). Bulk purge is chunked below
SQLite's bind-parameter limit.

## Purge protocol

Purge first commits logical deletion plus a payload-free, pseudonymous tombstone. It
retains a bundle-ID digest to prevent reuse and a purge timestamp for recovery; it
does not retain the plaintext ID or incident/session timing. Purge then unlinks only CAS
objects recorded by the deletion transaction and rechecks that no incident references
them. Unattributed orphans remain for explicit maintenance. Purge enables SQLite secure
deletion, checkpoints/truncates WAL, vacuums the database, fsyncs files, and fsyncs their
directory. If physical cleanup cannot complete, the durable tombstone and object queue
remain and the operation returns a retryable storage error. Repeating purge safely
retries cleanup.

This is best-effort file-level erasure. It cannot promise removal from snapshots,
backups, copy-on-write history, SSD remapped blocks, or storage-controller caches.
Cryptographic erasure requires encryption with disposable keys plus backup/snapshot
governance.

## Project deletion

Project deletion persists a `deleting` lifecycle state before removing project data.
Project writes check that state while holding the store mutation lock, so an ingest
cannot publish after the deletion fence. The API rejects project-data reads and writes
after the same durable state change. Each request removes at most 500 explicitly
selected incident rows. Once no incidents remain, it removes up to 500 rows from each
auxiliary table in order; incident deletion may also cascade additional graph, analysis,
and reference rows. The host retries the same request; the store-wide lock is released
between batches so other projects can continue using the shared store. When an incident
is removed, its CAS digest is entered into a project-scoped durable cleanup queue only
if no live incident references it. Each request unlinks at most 500 queued digests and
rechecks references under the mutation lock. A failed unlink leaves its queue row for
retry. Unknown crash-left CAS objects are not swept during project or incident deletion.

After the project's catalog, auxiliary rows, queued CAS objects, and owned capture
journals are gone, SQLite page compaction runs after the application mutation lock is
released. If capture-journal cleanup is pending, global compaction is deferred and the
project stays fenced in `deleting`; the API returns pending until every Earshot-owned
cleanup step completes. A busy checkpoint also keeps deletion pending. SQLite's own file
locks can still briefly serialize access during compaction. Bundle-ID hashes remain as
tombstones, and the project row remains in `deleted` state.

If file or database compaction cannot finish, the project stays fenced in `deleting`
and the API returns a retryable pending response. Retrying the same deletion is safe.
Earshot removes capture-call journals it owns. A checkpoint directory configured as an
external producer source remains producer-owned and must be erased by that producer's
owner. File-level erasure has the same backup, snapshot, and storage-device limits as
ordinary purge.

Durable browser-capture journals are paired with a project ownership sidecar. Earshot
does not extend a durable journal if that sidecar cannot be written. During project
erasure, unreadable sidecars and journals with no sidecar keep deletion pending because
their project ownership cannot be proven. An operator must restore the sidecar or
inspect and remove the unowned capture files before retrying erasure.

When a live capture expires, Earshot persists that state in the sidecar before dropping
its live view. Startup keeps the journal dormant and out of live-session quotas; an exact
client retry reattaches it lazily. Dormant, unfinalized calls still count against the
project's configured call limit, so restarting the service cannot bypass that bound.
Every unsealed durable call also counts against a server-wide durable-call limit, which
defaults to `LiveConfig.max_sessions` (32). The per-call journal byte limit defaults to
64 MiB, so the default limit bounds accumulated unsealed journal data to 2 GiB, plus the
small ownership sidecars. A successfully sealed call releases its journal slot after
the artifact and sealed replay marker are durable. Up to 1,024 sealed replay sidecars
are retained by default (`ApiConfig.max_sealed_capture_replay_ledgers`); pruning is
oldest-first, protects ledgers used by active retries, and only removes a sealed
sidecar after its journal is gone. The artifact store continues to reserve each bundle
ID after its replay sidecar is pruned, so a late retry returns
`409 EARSHOT_CAPTURE_REPLAY_EXPIRED` instead of recreating that call.
Current sidecars retain only a digest of the call's initial trace and clock metadata,
which lets retries prove continuity without copying those values into another record.

## Permissions and recovery

Data/object/temp directories are forced to mode `0700`; database, WAL/SHM, CAS objects,
`.store.lock`, and `.compaction.lock` are `0600`. The store lock serializes mutations;
the separate compaction lock serializes scrub and VACUUM work without holding up ordinary
store operations. Startup removes temporary files and drains project-attributed pending
ingest intents and incidents expired at each sweep's ingest-sequence high-water mark in
bounded lock intervals. The lifespan-managed maintenance sweep also retries pending
ingest cleanup left by transient unlink or catalog failures, in bounded batches; a
failed immediate cleanup is logged with its opaque intent ID. Later arrivals remain
hidden by expiry predicates for the lifespan-managed reaper. Startup verifies every remaining live artifact, checks
index/artifact identity, rebuilds derived projections, and retries compaction when a
durable scrub generation shows cleanup may have stopped after logical deletion. Older
catalogs with tombstones seed one conservative scrub generation during migration.

If CAS evidence exists while the SQLite catalog is missing, empty, corrupt, or not an
Earshot catalog, startup fails closed and preserves every object. Restore the catalog
from the same backup set before reopening. A valid catalog may still have a crash-left
unreferenced object if it predates the project-scoped intent journal or its ownership
record is corrupt; it is preserved until an operator explicitly invokes maintenance
cleanup after investigating it. New ingest publications are recoverable from
`pending_ingests`. Known artifact deletions are recorded in the project-scoped
`pending_object_deletions` queue before unlink, so retry does not need a global scan to
discover them.

The store is single-node. Advisory locking and SQLite are not a distributed
consensus protocol; a multi-node service should preserve these publication and erasure
semantics using its own transactional object/index infrastructure.

## Catalog compatibility and migration recovery

Catalog migration is forward-only. Startup refuses a catalog whose `user_version` is
newer than the binary and does not attempt to downgrade or modify it. For an older
catalog, all DDL, data copying, index replacement, and the final `user_version` update
run in one `BEGIN IMMEDIATE` transaction. A constraint failure or process exit therefore
leaves the old catalog intact; the next startup can retry the migration.

The evidence-backed historical layouts are:

- v1: the baseline Incident catalog;
- v2: derived analysis generation time stored as SQLite `INTEGER`;
- v3: purge tombstones containing plaintext bundle identifiers;
- v4: the first committed catalog, with hashed unscoped tombstones and graph indexes;
- v9: the next committed layout, adding Projects, scoped tombstones, Connectors,
  Delivery Receipts, External Identities, and wide Turn Facts; and
- v10: Turn Facts rebuilt with STT language as a fleet dimension; and
- v11: mutable cross-product references with project and incident ownership enforced by
  foreign keys;
- v12: project deletion without a durable per-artifact CAS cleanup queue;
- v13: project-scoped pending CAS deletion records; and
- v14: review markers for projects already deleting before the CAS ownership queue; and
- v17: monotonic incident-ingest ordering for bounded startup expiry sweeps; and
- v18: project-scoped CAS publication intents for interrupted ingest recovery.

Version numbers 5–8 were internal development markers folded into the v9 change. There
is no independently committed or released schema for those numbers, so Earshot does not
invent fixture definitions for them. Structural migration still recognizes the narrow
development-era Turn Fact projection and rebuilds it from canonical Incidents.

Before upgrading, stop Earshot and make a complete backup using the procedure below.
During migration, projects already marked `deleting` under v12 remain pending until an
operator takes a complete backup and runs the explicit store-wide sweep below, then
retries project deletion. The sweep removes objects unreferenced by every remaining
incident, accepts only regular objects under real two-character hexadecimal shard
directories, fsyncs each CAS shard, then clears the review markers. Symlinks or
unexpected entries stop the sweep and leave the review markers in place. It is explicit because
the old catalog no longer identifies which project owned each crash-left object.

```sh
earshot maintenance cleanup-unreferenced-objects \
  --data-dir /path/to/earshot-data \
  --confirm-global-object-sweep
```

This command can remove orphaned CAS objects from every project in that data directory.
Take and verify a complete backup first. A deleting project stays fenced and its API
continues to return pending until the sweep completes and the host retries deletion.

Derived graph and Turn Fact projections are rebuilt transactionally from canonical CAS
artifacts during startup. If any referenced artifact is missing, corrupt, or belongs to a
different Incident, reconciliation fails and rolls back every projection repair from that
attempt. Restore the correct CAS object and reopen the store to retry. Startup preserves
unreferenced objects because it cannot prove whether they are crash-left evidence or a
catalog restore mismatch.

## Backup and restore procedure

There is currently no online snapshot API. For a coherent backup:

1. Stop every Earshot process using the data directory and wait for it to close.
2. Copy the complete directory as one unit: `earshot.sqlite3` (and any WAL/SHM files),
   `objects/`, and `instance-correlation.key`.
   Also copy the complete `EARSHOT_CAPTURE_JOURNAL_DIR` when it is configured outside
   that directory; it contains in-flight call journals and their drain retry ledger.
3. Restore that complete unit into an empty directory; do not combine components from
   different backup times.
4. Open the restored directory with the same or a newer Earshot binary. Startup verifies
   catalog integrity, referenced CAS digests and identities, repairs rebuildable
   projections, and hardens file permissions.

Test the restored store before replacing the original. A missing catalog with CAS data,
or a populated catalog without its correlation key, fails closed with a restore
instruction. CAS alone cannot reconstruct ordering, tombstones, Projects, credentials,
or provider replay state.

`instance-correlation.key` has no supported in-place rotation seam. Delivery Receipt and
External Identity HMACs are deliberately non-reversible, and hosted bundle IDs are
deterministic HMACs of the project and idempotency key. Replacing the file would break
provider replay identity and hosted outbox retries, and the original provider identifiers
are unavailable for a correct rewrite. Keep the key under the same backup and access
controls as the catalog. A future rotation feature requires an explicit dual-key migration
protocol at provider-ingest time; until then, use a new empty store when a new correlation
domain is required.
