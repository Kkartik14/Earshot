# Storage and blobs

Earshot keeps two kinds of data:

- **The catalog**: Projects, API keys, Incidents and their projections, Calls, spans and
  Recording metadata. It lives in SQLite or Postgres.
- **Blobs**: Recording bytes. They live on local disk or in an S3-compatible store.

This page is the operator's guide: choosing a backend, migrations, upgrading from 0.1.x,
the correlation key and blob URLs. [Storage internals](./storage.md) describes how the
SQLite catalog and its content-addressed artifact store publish, purge and recover.
[Production notes](./production.md) cover topology and backups. Every variable is listed
in [server configuration](./configuration.md).

## Extras

| Extra      | Adds                                                         | Needed for                                             |
| ---------- | ------------------------------------------------------------ | ------------------------------------------------------ |
| `server`   | FastAPI, uvicorn, SQLAlchemy, Alembic, `opentelemetry-proto` | `earshot serve` and every command that opens the store |
| `postgres` | `psycopg[binary]`                                            | a `postgresql://` database                             |
| `s3`       | `boto3`                                                      | an `s3://` blob URL                                    |

```bash
pip install 'earshot-observability[server,postgres,s3]'
```

The base install is the SDK only. Since 0.2.0, every command that opens the store needs
the `server` extra: `serve`, `list`, `ingest`, `show`, `purge`, `project`, `api-key`,
`connector`, `maintenance`, `db` and `recover --ingest`. Without it they exit with status 2
and name the extra. `validate`, `query`, `contradictions`, `diff`, `export` and
`checkpoints` still work on a base install.

## Choose a database

|                    | SQLite (default)                                    | Postgres                                                                               |
| ------------------ | --------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Select it          | Leave `EARSHOT_DATABASE_URL` unset                  | `EARSHOT_DATABASE_URL=postgresql://user:password@host:5432/db`                         |
| Catalog            | `<data-dir>/earshot.sqlite3`                        | The database                                                                           |
| Incident artifacts | Files under `<data-dir>/objects/`                   | Rows in the `artifacts` table                                                          |
| Correlation key    | `<data-dir>/instance-correlation.key`, created once | `EARSHOT_CORRELATION_KEY` or `_FILE`, required                                         |
| Writers            | One server, plus CLI processes on one host          | One server, plus CLI processes from anywhere                                           |
| Erasure            | Secure delete, WAL truncation and VACUUM            | Rows deleted; physical erasure is delayed (see [privacy](./privacy.md#erasure-limits)) |

`--database-url` on any store command overrides `EARSHOT_DATABASE_URL`. The schemes
`postgresql://`, `postgres://` and `postgresql+psycopg://` are accepted. Earshot never
logs the password: it prints the URL as `postgresql://user:***@host:5432/db`.

### Set up Postgres

Give Earshot an empty database. It refuses a schema that holds tables it did not create.
Migrations need `CREATE` on the schema and run in one transaction.

```sql
CREATE ROLE earshot LOGIN PASSWORD '<password>';
CREATE DATABASE earshot OWNER earshot;
```

```bash
export EARSHOT_DATABASE_URL='postgresql://earshot:<password>@db.internal:5432/earshot'
export EARSHOT_CORRELATION_KEY="$(openssl rand -base64 32)"   # store it as a secret
earshot db upgrade
earshot serve
```

Every CLI command against that database needs the same URL, the `postgres` extra and the
same correlation key. CLI writes are safe while the server runs: they wait for the
server's current write, then run.

### How Postgres differs from SQLite

- **One server per database, across hosts.** A second server anywhere, or a second
  uvicorn worker, is refused with `EARSHOT_DATABASE_IN_USE`. SQLite refuses a second
  server on the same data directory with a file lock, which only processes on one host
  see. See [production](./production.md#one-server-one-worker).
- **No transaction-pooling proxy.** PgBouncer in transaction or statement mode breaks
  Earshot's session advisory locks. Session mode works.
- **Artifacts live in the database.** Nothing is written to the data directory. The
  database, its WAL and its backups grow with your evidence.
- **Connections.** A pool of `EARSHOT_DB_POOL_SIZE` (default 8, at least 2) with a 5 s
  checkout timeout; a timeout answers 503 `EARSHOT_STORAGE_UNAVAILABLE`. Each open store
  also holds one write-lock session, and the server one server-lock session. Their
  application names are `earshot`, `earshot-write-lock` and `earshot-server-lock`.
- **No `user_version`.** `earshot db current` reports `head`, `revision` and `state`.
- **Weaker erasure.** Purge deletes rows, artifact bytes included, in one transaction.
  There is no VACUUM-style scrub. See [privacy](./privacy.md#erasure-limits).
- **Session pages** are a top-N sort over that session's rows rather than an index scan.
- **`earshot maintenance cleanup-unreferenced-objects`** deletes unreferenced artifact
  rows, which is normally none.
- **Tested on Postgres 16.** Earshot uses advisory locks, `BIGSERIAL`, `COLLATE "C"`,
  partial and expression indexes, and transactional DDL.

## Migrations

Alembic owns the schema on both databases. It is configured in code; there is no
`alembic.ini` to edit. Migrations are forward-only.

| Command                        | What it does                                                                                                                                                    |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `earshot db current`           | Reports `head`, `revision` and `state` (`empty`, `legacy`, `behind`, `current` or `unsupported`) without changing anything. SQLite also reports `user_version`. |
| `earshot db upgrade`           | Migrates to this release's head and prints the revision before and after.                                                                                       |
| `earshot db reproject [--all]` | Re-projects Incidents whose read models predate this release. `--all` re-projects and verifies every Incident, for example after a restore.                     |
| `earshot serve`                | Migrates on start. With `--no-auto-migrate` it refuses a schema that is behind, with `EARSHOT_SCHEMA_BEHIND`.                                                   |

A schema newer than the binary is refused with `EARSHOT_SCHEMA_TOO_NEW` and left untouched.
To make migrations an explicit step, run `earshot db upgrade` from your deploy job and
start the server with `--no-auto-migrate`. The ASGI app (`uvicorn apps.ingest.app:app`)
always migrates on start.

## Upgrading from 0.1.x

0.2.0 and later upgrade SQLite data directories written by 0.1.x. This is tested against real
catalogs written by 0.1.0 (schema v10) through the last 0.1.x commit (schema v19): row
counts and artifact digests survive, and the result matches a fresh install.

1. Stop every Earshot process that uses the data directory.
2. Back it up as one unit, as in [the backup procedure](./storage.md#backup-and-restore-procedure).
3. Install the current release with the server extra.
4. Check the state, then upgrade:

   ```bash
   earshot db current --data-dir /path/to/earshot-data    # state: legacy
   earshot db upgrade --data-dir /path/to/earshot-data
   ```

   Starting `earshot serve` does the same.

5. Start the server. Incidents are re-projected in the background, and `/readyz` reports
   `projections_pending` until they finish. Without a server, run `earshot db reproject`.
6. Choose each Project's Capture Preset. The upgrade sets **every existing Project to
   `metadata_only`** and logs `EARSHOT_LEGACY_CAPTURE_PRESET` with the count, so an
   upgrade never widens what you store. To keep transcripts and recordings for a Project:

   ```bash
   earshot project set-capture --project default --preset full
   ```

**The upgrade is one-way.** Afterwards every 0.1.x binary refuses the file as "newer than
this binary". To go back, restore the backup. A crash during the upgrade is safe: the next
open finishes it.

Incidents written by 0.1.0 itself could not be read by any later 0.1.x build. 0.2.0 reads
them again. Re-ingesting such an Incident under the same bundle ID is still a conflict,
because its bytes are now encoded differently.

There is no command in 0.2.0 that copies a SQLite catalog into Postgres. Moving to Postgres
starts an empty database; keep the SQLite data directory for the history it holds.

## The correlation key

Earshot derives non-reversible fingerprints with an instance key: Connector delivery
receipts, External Identities, the IDs it mints for hosted artifacts, and Call IDs and
Session Key fingerprints. On SQLite the key is `instance-correlation.key` in the data
directory, created on first open. On Postgres you supply it:

- `EARSHOT_CORRELATION_KEY`: 32 bytes, base64-encoded; or
- `EARSHOT_CORRELATION_KEY_FILE`: a file holding the 32 bytes raw or base64-encoded. It
  must be mode `0600` and not a symbolic link.

Without a usable key, the store refuses with `EARSHOT_CORRELATION_KEY_REQUIRED`. The
database stores only a one-way key ID. A different key is refused with
`EARSHOT_CORRELATION_KEY_MISMATCH`, and a lost key cannot be recovered from the database:
back it up with the database, under the same access controls. There is no key rotation.

**Carrying the key over from SQLite.** To keep the same fingerprints and minted IDs when
you move from SQLite to Postgres, point the file variable at the old key:

```bash
cp /path/to/earshot-data/instance-correlation.key /etc/earshot/correlation.key
chmod 600 /etc/earshot/correlation.key
export EARSHOT_CORRELATION_KEY_FILE=/etc/earshot/correlation.key
```

The new database then produces the same External Identity fingerprints, hosted bundle IDs
and Call IDs as the SQLite store did for the same inputs.

## Blob URLs

`EARSHOT_BLOB_URL` chooses where Recording bytes go. It is validated at start-up. Recording
uploads are the only user of the blob store in 0.2.0.

| URL                                                               | Backend                          |
| ----------------------------------------------------------------- | -------------------------------- |
| unset                                                             | Local disk at `<data-dir>/blobs` |
| `file:///var/lib/earshot/blobs`                                   | Local disk at an absolute path   |
| `s3://bucket/prefix?region=eu-west-1`                             | Amazon S3                        |
| `s3://bucket/prefix?endpoint=https://s3.example.com&path_style=1` | An S3-compatible store           |

**Local disk.** The path must be absolute and percent-encoded where needed (as
`Path.as_uri()` writes it). An upload is written to `<root>/.tmp` on the same filesystem,
fsynced, then renamed into place, so a reader sees the whole object or nothing.

**S3.** The query options are `endpoint` (an `http` or `https` URL), `region` and
`path_style` (`0` or `1`), each at most once. Earshot uses boto3 with SigV4. An upload
smaller than 8 MiB is one `PutObject`; a larger one is a sequential multipart upload in
8 MiB parts, so memory stays flat. Earshot computes the sha256 itself and sends the SDK's
extra checksum headers only when the store requires them, because some S3-compatible
stores reject them.

**Credentials come from the standard AWS variables** (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE`) or boto3's usual credential
chain, such as an instance role. A URL that carries credentials or a `#fragment` is refused,
and the refusal never repeats the value. Earshot needs `s3:PutObject`, `s3:GetObject`,
`s3:DeleteObject` and `s3:AbortMultipartUpload` on the prefix, and
`s3:ListBucketMultipartUploads` and `s3:ListBucket` on the bucket. Without `s3:ListBucket`,
S3 reports a missing object as forbidden rather than missing.

Blob keys are generated by the server, one per upload attempt:
`recordings/<project_id>/<attempt_uuid>`. Client strings never appear in keys. Write
nothing else under Earshot's prefix.

When it starts, and hourly after that, Earshot removes what a crashed upload left: spool
files on local disk, and unfinished multipart uploads under the prefix on S3. Uploads in
progress are spared. The sweep runs in the background, so an unreachable S3 does not stop
the server from starting; a failed sweep is logged as `EARSHOT_BLOB_SWEEP_FAILED` and
retried a minute later. Add an S3 lifecycle rule as a second line of defence; see
[production](./production.md#s3-lifecycle-rule).

### MinIO for local testing

The test suite and CI use the Chainguard MinIO image. It has no default command, so pass
`server /data`:

```bash
export MINIO_ROOT_USER=earshot-dev
export MINIO_ROOT_PASSWORD="$(openssl rand -hex 24)"
docker run --detach --name earshot-minio \
  --publish 127.0.0.1:9000:9000 \
  --env MINIO_ROOT_USER --env MINIO_ROOT_PASSWORD \
  cgr.dev/chainguard/minio:latest server /data
```

Create a bucket and point Earshot at it:

```bash
export AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER"
export AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
export AWS_DEFAULT_REGION=us-east-1
python -c "import boto3; boto3.client('s3', endpoint_url='http://127.0.0.1:9000').create_bucket(Bucket='earshot')"
export EARSHOT_BLOB_URL='s3://earshot/blobs?endpoint=http://127.0.0.1:9000&region=us-east-1&path_style=1'
earshot serve
```

Use `path_style=1` with MinIO. To run the blob-store tests against it, set
`EARSHOT_TEST_S3_URL=http://<user>:<password>@127.0.0.1:9000` and run
`pytest apps/ingest/tests/test_blobstore.py`.
