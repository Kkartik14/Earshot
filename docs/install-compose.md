# Install with Docker Compose

<!-- Each block after an "earshot-step" comment is run verbatim by the install
tests (apps/ingest/tests/install) and by scripted first-run timings. Keep every
marker directly above its block, and keep the block runnable as written. -->

Earshot ships as one container image, `ghcr.io/kkartik14/earshot`, serving the API and
the viewer on port 4319. The repository has three Compose files:

| File                | What runs                                                      | Use it for                                        |
| ------------------- | -------------------------------------------------------------- | ------------------------------------------------- |
| `compose.yaml`      | Earshot alone, with SQLite and recordings on a named volume    | Trying Earshot; a single-machine install          |
| `compose.full.yaml` | Earshot, Postgres 17 and MinIO (S3-compatible recordings)      | A team install on one host, with a required token |
| `compose.dev.yaml`  | Adds `build: .` to either file, so the image comes from source | Running a checkout                                |

Both installs publish Earshot on host loopback only (`127.0.0.1:4319`), run every
container with a read-only root filesystem, no Linux capabilities and no privilege
escalation, and restart it if it exits. You need Docker Engine with Compose v2.24 or
later.

## Quick start

Download the Compose file into an empty directory:

<!-- earshot-step: quickstart-download -->

```bash
curl -fsSLO https://raw.githubusercontent.com/Kkartik14/Earshot/v0.4.0/compose.yaml
```

Start Earshot and wait until it reports ready:

<!-- earshot-step: quickstart-up -->

```bash
docker compose up -d --wait
```

Open <http://127.0.0.1:4319>. Agents export OTLP/HTTP to `http://127.0.0.1:4319` (the
exporter appends `/v1/traces`); the [OTLP quick start](./otlp-quickstart.md) covers
exporters, session keys and recordings.

The API answers without a credential here, because the port is reachable from this
machine only (`EARSHOT_TRUST_LOCAL_NETWORK`). Never publish it on another interface.

### Settings

Compose reads an optional `.env` file next to `compose.yaml`, and passes its variables to
the server. Use it for:

- `EARSHOT_TOKEN=<a high-entropy secret>` to require a credential on every `/v1` request;
- the webhook secrets a connector names as `env:NAME`;
- any other variable from [server configuration](./configuration.md).

To publish on another host port, set `EARSHOT_PUBLISHED_PORT=4320` in `.env` or in your
shell. Run `docker compose up -d --wait` again after a change.

### Stop, restart and delete

Stop Earshot. Calls and recordings stay in the `earshot-data` volume, and
`docker compose up -d --wait` brings them back:

<!-- earshot-step: quickstart-stop -->

```bash
docker compose down
```

Delete Earshot and everything it stored:

<!-- earshot-step: quickstart-delete -->

```bash
docker compose down --volumes
```

### Upgrade

`compose.yaml` follows the `0.4` image tag, which receives patch releases. Back up first
(the SQLite procedure in [storage](./storage.md#backup-and-restore-procedure)), then:

```bash
docker compose pull
docker compose up -d --wait
```

For a new minor release, download that release's `compose.yaml`.

### Without a registry

For a host without access to `ghcr.io`, or an image you built yourself, load the image
from a tarball. Compose uses a local image with the tag it names and does not pull:

```bash
docker save ghcr.io/kkartik14/earshot:0.4 -o earshot-0.4.tar   # where the image is
docker load -i earshot-0.4.tar                                   # on the target host
docker compose up -d --wait
```

## Postgres and MinIO

`compose.full.yaml` runs Earshot with a Postgres 17 catalog and recordings in MinIO, and
requires a token on every `/v1` request. Download it into an empty directory:

<!-- earshot-step: full-download -->

```bash
curl -fsSLO https://raw.githubusercontent.com/Kkartik14/Earshot/v0.4.0/compose.full.yaml
```

Generate the four secrets into `.env`. Keep this file, and back it up with your data:
Earshot derives Call IDs and fingerprints from the correlation key, and a lost key cannot
be recovered.

<!-- earshot-step: full-env -->

```bash
umask 077
cat > .env <<EOF
EARSHOT_TOKEN=$(openssl rand -hex 32)
EARSHOT_CORRELATION_KEY=$(openssl rand -base64 32)
POSTGRES_PASSWORD=$(openssl rand -hex 24)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
EOF
```

Start the stack. Earshot starts once Postgres and MinIO report healthy:

<!-- earshot-step: full-up -->

```bash
docker compose -f compose.full.yaml up -d --wait
```

Open <http://127.0.0.1:4319> and sign in with the token (`grep EARSHOT_TOKEN .env`).
Agents send it, or a project API key created with it, as `Authorization: Bearer <key>`.

| Service    | Image                                 | Notes                                                                                                          |
| ---------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `earshot`  | `ghcr.io/kkartik14/earshot:0.4`       | Published on `127.0.0.1:4319`. Other containers on the Compose network reach `http://earshot:4319` with a key. |
| `postgres` | `postgres:17`                         | Not published. Checked with `pg_isready` over TCP. Data in the `postgres-data` volume.                         |
| `minio`    | `cgr.dev/chainguard/minio`, by digest | Not published. Checked with `mc ready local`. Creates the `earshot` bucket at start. Data in `minio-data`.     |

Postgres and MinIO passwords go into URLs and variables as written: keep them to letters
and digits, as the generator above does.

**Backups.** Back up the database, the recordings and `.env` together; see
[production](./production.md#backups) for the order. Dump the database while the stack
runs:

<!-- earshot-step: full-backup-catalog -->

```bash
docker compose -f compose.full.yaml exec -T postgres \
  pg_dump --username earshot --format=custom earshot > earshot.dump
```

Then copy the recordings: stop the stack (`docker compose -f compose.full.yaml down`) and
copy the `minio-data` volume, or mirror the `earshot` bucket with an S3 client
(`mc mirror`, `aws s3 sync`) through a port you publish on loopback.

**Exposing it.** To serve other machines, put a TLS-terminating proxy in front, and in
`compose.full.yaml` replace `EARSHOT_TRUST_LOCAL_NETWORK: "true"` with
`EARSHOT_BEHIND_TLS_PROXY: "true"`. The token stays required either way.

**Stopping and deleting** work as in the quick start, with `-f compose.full.yaml`.

## Build from a checkout

`compose.dev.yaml` adds a build of this checkout to either file. The result is tagged
`earshot:dev`, so it never stands in for a release tag:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build --wait
docker compose -f compose.full.yaml -f compose.dev.yaml up -d --build --wait
```
