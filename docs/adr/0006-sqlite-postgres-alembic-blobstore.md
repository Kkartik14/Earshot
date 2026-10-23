# ADR 0006: Run SQLite or Postgres under one Alembic migration system, with a BlobStore for media

Status: accepted (2026-10-19)

## Context

Through 0.1.x, Earshot stored everything in SQLite plus a file content-addressed store.
`storage.py` holds hand-written SQL and one idempotent "converge" routine that brings any
file from v1 to v19, tracked by `PRAGMA user_version`.

Month 1 needs all of these at once:

- `earshot serve` still works with one command on a laptop (SQLite plus local disk);
- teams and production run on Postgres and an S3-compatible store;
- real 0.1.x databases (schemas v10 to v19) upgrade without losing data;
- audio, far too large for catalog rows, goes to local disk or S3.

## Decision

**Configuration.** `EARSHOT_DATABASE_URL` (or `--database-url`) selects the database. If
it is absent, SQLite at `<data-dir>/earshot.sqlite3` is used, unchanged; a
`postgresql://…` URL selects psycopg 3. `EARSHOT_BLOB_URL` selects the blob store.

**One schema, one migration system.** The DDL is defined once as SQLAlchemy `MetaData`,
and Alembic runs it on both dialects. Alembic is configured in code: revisions are plain
`.py` files, with no `alembic.ini` and no `.mako` template. The baseline
`0001_v19_baseline` is the v19 schema minus the `incidents_track_ingest_order` trigger,
which becomes an explicit insert on both dialects.

- A dialect-aware catalog snapshot (columns and types, keys, indexes, CHECKs, no
  triggers) must be equal for a fresh SQLite file, an upgraded SQLite file and a fresh
  Postgres database.
- Integer nanosecond columns are BIGINT on Postgres. Decimal-text nanosecond columns stay
  TEXT with `COLLATE "C"`. REAL becomes double precision.
- `earshot db upgrade` and `earshot db current` run and report migrations. `serve`
  migrates automatically unless `--no-auto-migrate` is given.

**Legacy bootstrap and the 1000 sentinel.** A SQLite file with no `alembic_version` and
a `user_version` of 19 or less is a 0.1.x catalog. Earshot runs the frozen 0.1.x converge,
stamps the baseline, sets `PRAGMA user_version = 1000`, and upgrades to head. The
sentinel means "managed by Alembic": every 0.1.x binary then refuses the file with its
existing newer-schema error. Other states also refuse:

- an `alembic_version` this binary does not know: `EARSHOT_SCHEMA_TOO_NEW`;
- a legacy `user_version` from 20 to 999: the existing newer-schema error.

Migrations run with foreign keys off, and `PRAGMA foreign_key_check` must be empty before
commit. A crash between the bootstrap commit and the stamp is completed on the next open.
Upgrade fixtures come from running the historical code (`v0.1.0`, `553cecf`, `86c4c9c`,
`3c2f0b3`). Each must upgrade with its row counts and artifact digests unchanged and end
with the fresh-install catalog.

**Runtime: hand-written SQL through a shim.** Queries stay hand-written SQL with `?`
placeholders, run through a `Database` shim. SQLAlchemy supplies the pool and dialect
handling; it never builds queries. The shim:

- translates `?` to `%s` for psycopg, escaping literal `%`;
- owns the transaction context managers: commit on a clean exit, roll back on an
  exception, and never hand out the pool's proxy connection (sqlite3's `with` commits,
  while a pool proxy's `with` rolls back);
- draws from a pool of `EARSHOT_DB_POOL_SIZE` connections (default 8) with a 5 s
  timeout. A pool timeout maps to 503.

SQLAlchemy and Alembic live in the `[server]` extra and are imported lazily, so the base
SDK install never imports them. psycopg is in `[postgres]` and boto3 in `[s3]`.

Rejected alternatives:

- **An ORM rewrite.** The store's behaviour lives in transaction boundaries and ordering,
  not in SQL syntax. Rewriting every query puts that behaviour at risk for no gain.
- **SQLAlchemy Core `text()` everywhere.** Too much churn across the store for what a
  placeholder shim already gives.
- **A calls-only Postgres store** beside SQLite. Split brain: two databases and two
  migration systems for one catalog.

**Single writer.** Month 1 runs one writer per database.

- **`K_server`** is a Postgres session advisory lock taken only by `serve`, under both
  entry points, on a dedicated non-pooled connection with TCP keepalives. A second
  server, or a second uvicorn worker, gets `EARSHOT_DATABASE_IN_USE` and exits non-zero.
  A watchdog checks the lock every closer tick. If it is lost, the process logs
  `EARSHOT_DATABASE_LOCK_LOST` and calls `os._exit(70)`; it does not try to re-acquire.
- **`K_write`** is a session-level `pg_advisory_lock` held for the outermost
  `_mutation()`, in any process, server or CLI. It is the Postgres analogue of SQLite's
  per-mutation `flock`, and it also serializes mutations that span several transactions,
  such as project deletion. Every write goes through `_mutation()`, so a CLI command like
  `earshot api-key revoke` works while the server runs. SQLite keeps its `flock`.
- Operators run one worker per server and no transaction-pooling proxy (PgBouncer in
  transaction mode).

**Postgres specifics.**

- Artifacts are BYTEA rows in `artifacts`, written in the catalog's transaction and read
  with binary transfer. Liveness is derived, not refcounted: a purge deletes rows whose
  digest no `incidents.object_digest` references. SQLite keeps the file CAS.
- The correlation key is required, as `EARSHOT_CORRELATION_KEY` or
  `EARSHOT_CORRELATION_KEY_FILE`; without it the server refuses with
  `EARSHOT_CORRELATION_KEY_REQUIRED`. The database stores only a key ID, and a different
  key gives `EARSHOT_CORRELATION_KEY_MISMATCH`. Correlation must survive a move between
  hosts (ADR 0002), and the docs explain how to carry the SQLite key over.

**BlobStore.** Media goes to one small interface with two backends.

- The interface: `begin_put(key)` returns a writer with `write(chunk)`,
  `commit() -> (sha256, size)` and `abort()`, plus `open_range`, `head` and `delete`.
- `file://…`, by default `<data-dir>/blobs`, spools to `<blob root>/.tmp` on the same
  filesystem, so the final rename is atomic.
- `s3://bucket/prefix?endpoint=…&region=…&path_style=1` uses the standard AWS credential
  variables, boto3 with `signature_version="s3v4"`, and a manual, sequential multipart
  upload in 8 MiB parts.
- sha256 is computed while streaming. A mismatching `Content-Digest` gives 400 and
  nothing commits. An abort leaves no visible object, and a startup sweep removes what a
  crash left behind.
- Recording keys are generated by the server per upload attempt
  (`recordings/<project_id>/<attempt_uuid>`). Client strings never appear in keys.
- A 1 GiB upload may grow the server's peak RSS by at most 64 MiB.

**Deferred to Month 3.** Multiple replicas and multiple workers need a concurrency
redesign, not a driver swap. They arrive with Helm in Month 3, along with the
multi-writer concurrency test.

## Consequences

Positive:

- The laptop path is unchanged, and teams get Postgres and S3. The schema is defined
  once and tested equal on both dialects.
- Real 0.1.x data upgrades, tested against files written by the historical code.
- CLI administration is safe while the server runs, and losing the server lock fails
  fast instead of risking two writers.

Negative:

- One writer per database caps write throughput and rules out high availability until
  Month 3.
- The design is crash-only: losing the lock drops in-flight requests, so the server
  needs a supervisor that restarts it.
- PgBouncer in transaction mode is not supported, and the upgrade is one-way: once
  stamped, a file is refused by every 0.1.x binary.
- BYTEA artifacts grow the database, its WAL and its backups, and erasure on Postgres is
  weaker (ADR 0004).
- The correlation key is one more secret to back up. Losing it loses correlation.
- Storage-backed CLI commands now need `[server]`, a breaking change in 0.2.0.

## Evidence

The storage behavior is documented in [Storage](../storage.md) and
[Storage and blobs](../storage-and-blobs.md). The upgrade fixtures under
`fixtures/upgrade/` identify the historical release commits used by their tests.
The implementation lives in `packages/sdk-python/src/earshot/`.
