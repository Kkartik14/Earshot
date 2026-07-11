# Storage, graph indexing, retention, and purge

The local backend uses two coordinated stores:

```text
canonical evidence: objects/sha256/<2 hex>/<62 hex>
derived/index state: earshot.sqlite3
```

The canonical deterministic protobuf is the source of truth for evidence content and
graph facts. SQLite is the durable authority for publication, bundle identity,
ingest order, retention/export decisions, and purge tombstones. The two stores form
one persistence unit and must be backed up and restored together; CAS bytes alone
cannot reconstruct deleted-ID tombstones or original ingest ordering.

## Ingest publication order

All filesystem and database mutations are serialized by a process `RLock` and, on
Unix, an advisory file lock shared by backend processes using the same directory.

An ingest does the following inside that mutation boundary:

1. Validate the structural, semantic, graph, privacy, and hash contract.
2. Re-encode and compare any caller-supplied canonical payload.
3. Write a temporary object, flush and fsync it.
4. Atomically hard-link it to its SHA-256 path and fsync the containing directories.
5. Begin an immediate SQLite transaction.
6. Reject tombstone reuse or a same-ID/different-digest conflict.
7. Insert the incident row, graph projections, earliest expiry, and destination export
   decisions.
8. Commit before returning `created=true`.

The CAS object remains inside the same cross-process critical section until the index
commit. Cleanup therefore cannot mistake an in-flight object for an orphan. On a
database failure, an unreferenced object is removed when the same ingest can prove it
created that object and no committed row references it. Startup never guesses that an
unreferenced CAS object is disposable.

## Relational projections

Schema version 4 indexes:

- `incidents`: identity, digest, status, finality/completeness, framework, creation and
  ingest times, earliest expiry, and `local_api`/`local_cli` export decisions;
- `operations`: normalized OTel identity, parentage, participant/stream/turn, source
  and monotonic boundaries, evidence summary, and capture class;
- `causal_links`: ordered typed edges from an operation to internal/external targets;
- `events`: point identity, operation/trace correlation, participant/stream/turn,
  timestamps, evidence summary, and capture class;
- `analyses`: analyzer version + exact input digest + strict JSON output; full uint64
  generation times are canonical decimal `TEXT`, not signed SQLite integers; and
- `tombstones`: only `SHA-256(bundle_id)` and the purge-operation time.

Foreign keys cascade graph and analysis rows when an incident is deleted. Graph rows
are derived and rebuilt on startup, which also backfills retention/export projections
when an older database is migrated.

## Idempotency and concurrency

The tuple `(bundle_id, canonical_digest)` defines an exact retry. The same pair returns
the existing row. Reusing a bundle ID with different content is a conflict. A purged
bundle ID can never be reused.

Reads verify CAS bytes against the indexed digest. Artifact read and concurrent purge
share the mutation boundary, so a successful read cannot race an unlink into a false
success. Missing or mismatched bytes are corruption, not a not-found response.

## Retention

Each captured class may declare an absolute `expires_at_unix_nano`, a `ttl_nano`
relative to immutable bundle creation, or both. Earshot stores the earliest deadline
across all captured classes. Selective in-place deletion would change the artifact and
digest, so the strictest class expires the whole bundle.

`purge_expired(now, limit)` is available for explicit maintenance. Enforcement is also
automatic:

- during store startup;
- before record/artifact/analysis reads; and
- before incident listings.

Thus an expired artifact is not served while waiting for a background scheduler.
Bulk purge is chunked below SQLite parameter limits.

## Purge protocol

Purge first commits logical deletion plus a payload-free, pseudonymous tombstone. It
retains a bundle-ID digest to prevent reuse and a purge timestamp for recovery; it
does not retain the plaintext ID or incident/session timing. Purge then unlinks
CAS objects no longer referenced by another incident, removes other orphans, enables
SQLite secure deletion, checkpoints/truncates WAL, vacuums the database, fsyncs files,
and fsyncs their directory. If physical cleanup cannot complete, the durable tombstone
remains and the operation returns a retryable storage error. Repeating purge safely
retries cleanup.

This is best-effort file-level erasure. It cannot promise removal from snapshots,
backups, copy-on-write history, SSD remapped blocks, or storage-controller caches.
Cryptographic erasure requires encryption with disposable keys plus backup/snapshot
governance.

## Permissions and recovery

Data/object/temp directories are forced to mode `0700`; database, WAL/SHM, CAS objects,
and the shared lock are `0600`. Startup removes temporary files, verifies every live
artifact, checks index/artifact identity, rebuilds derived projections, and repeats the
secure scrub if tombstones show a process may have stopped between logical deletion and
compaction.

If CAS evidence exists while the SQLite catalog is missing, empty, corrupt, or not an
Earshot catalog, startup fails closed and preserves every object. Restore the catalog
from the same backup set before reopening. A valid catalog may still have a crash-left
unreferenced object; it is preserved until an operator explicitly invokes the
maintenance cleanup after investigating it.

The store is single-node and local. Advisory locking and SQLite are not a distributed
consensus protocol; a multi-node service should preserve these publication and erasure
semantics using its own transactional object/index infrastructure.
