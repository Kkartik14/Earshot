# Deploy on Railway

A one-click Railway template is planned; until it is published, this page is both its
specification and the manual setup. The repository's check covers
`deploy/railway/railway.toml` against Railway's published schema only; a real deploy and
redeploy has not been verified yet.

## What the template creates

Two services in one project:

1. **Postgres**, Railway's PostgreSQL service.
2. **Earshot**, from the image `ghcr.io/kkartik14/earshot:0.4`, or from this repository
   with its config file path set to `deploy/railway/railway.toml` (which builds the
   `Dockerfile`). Settings, the same in both cases:

   | Setting          | Value                                         |
   | ---------------- | --------------------------------------------- |
   | Volume           | **mounted at `/data`; mandatory** (see below) |
   | Replicas         | 1                                             |
   | Healthcheck path | `/readyz`, timeout 300 s                      |
   | Restart policy   | on failure, up to 10 times                    |
   | Draining         | 15 s                                          |
   | Public domain    | HTTP, to port 4319                            |

   Variables:

   | Variable                   | Value                                                                                  |
   | -------------------------- | -------------------------------------------------------------------------------------- |
   | `EARSHOT_HOST`             | `0.0.0.0`                                                                              |
   | `PORT`                     | `4319`                                                                                 |
   | `EARSHOT_BEHIND_TLS_PROXY` | `true` (Railway's edge terminates TLS)                                                 |
   | `EARSHOT_TOKEN`            | `${{secret(64, "abcdef0123456789")}}`                                                  |
   | `EARSHOT_DATABASE_URL`     | `${{Postgres.DATABASE_URL}}`                                                           |
   | `EARSHOT_CORRELATION_KEY`  | `${{secret(43, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/")}}=` |
   | `RAILWAY_RUN_UID`          | `0`                                                                                    |

Recordings are stored as files on the volume (`/data/blobs`). To keep them in S3 instead,
add `EARSHOT_BLOB_URL` and the AWS credential variables; see
[storage and blobs](./storage-and-blobs.md#blob-urls).

After the deploy, sign in at the service's public domain with `EARSHOT_TOKEN` (shown in
the service's variables). Every `/v1` request needs it, or a project API key created
with it. Copy `EARSHOT_CORRELATION_KEY` somewhere safe: Earshot derives Call IDs and
fingerprints from it, and a lost key cannot be recovered.

## Why the volume is mandatory

Earshot runs one server per database: the server holds a Postgres advisory lock while it
runs, and a second server exits with `EARSHOT_DATABASE_IN_USE`. Railway normally starts a
new deployment beside the old one and switches over once the new one passes its health
check. Railway never keeps two deployments of a service with a volume active at once, so
with the volume, a redeploy stops the old server first and the new one takes the lock.
Without a volume, the new deployment would fail its health check against the old
server's lock, and every redeploy would fail. `railway.toml` also sets
`requiredMountPath = "/data"`, which Railway's schema describes as the deployment's
required mount path; the first real deploy should confirm that Railway refuses to deploy
without the volume.

Railway mounts volumes owned by root, and the image runs as a non-root user; Railway's
documented fix is `RAILWAY_RUN_UID=0`, which runs the container as root so it can write
to the volume.

## Publishing the template (maintainers)

Compose the two services above in Railway's template editor, attach the volume, set the
variables, and publish. Then deploy it once, redeploy once, and confirm both deployments
pass their health checks and the second one keeps the calls of the first. Also try a
deployment without the volume, and record whether `requiredMountPath` refuses it.
