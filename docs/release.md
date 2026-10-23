# Python release packaging

The installable distribution and Python import intentionally have different names:

```text
distribution: earshot-observability
import:       earshot
CLI:          earshot
```

The [PyPI project named `earshot`](https://pypi.org/project/earshot/) is an unrelated
Rust-backed VAD package. It has already published overlapping versions and also
installs an `earshot` import package. Never upload this repository under that
distribution name, and do not install both projects into one environment because
their import namespaces collide.

The release publishes both a wheel and source distribution. Both artifacts carry the
same compiled viewer produced immediately before the Python build. Hatch has explicit
wheel rules and an sdist source allowlist. The sdist checker streams the archive and
independently rejects paths outside that manifest; forbidden development/private path
segments at any depth; anything under `earshot/web` except `index.html` and direct
compiled `.js`/`.css` assets; more than 8 MiB compressed content, 1,024 total members,
1 MiB of tar header metadata, 512 files, or 32 MiB unpacked content; and archives missing
the viewer, generated protobuf module, package entry point, or required source files. CI
adds a forbidden dirty-tree sentinel before building, so an accidental return to
VCS-wide sdist discovery fails the artifact lane.

The base install is the SDK and evidence core only:

```bash
python -m pip install earshot-observability
```

The server extra adds the local API, the bundled viewer, and every CLI command that opens
the store (`serve`, `list`, `ingest`, `db`, `project`, `api-key` and the rest). Since 0.2.0
those commands exit with status 2 on a base install. The `postgres` and `s3` extras add the
Postgres driver and the S3 client:

```bash
python -m pip install 'earshot-observability[server]'
python -m pip install 'earshot-observability[server,postgres,s3]'
```

`earshot-observability` returned no public PyPI project when this decision was made
on 2026-07-13. A missing project is not a reservation: an owner must reserve the name
before release, and the release operator must recheck ownership immediately before
uploading. If the name cannot be reserved, stop and make an explicit distribution
rename; do not silently fall back to `earshot`.

## Reproducible release check

Run the same gates as CI from a full clone (the upgrade tests extract earlier releases
from git history). The viewer, wheel, and sdist are one artifact graph; do not build or
upload the Python archives without the viewer bundle step:

```bash
python scripts/generate_contract.py --check
python scripts/generate_openapi.py --check
python scripts/generate_fault_fixtures.py --check
python scripts/check_semconv.py
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples conftest.py
ruff format --check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples conftest.py
COVERAGE_FILE=.coverage.sqlite pytest --cov=earshot --cov-report= --cov-fail-under=0 -q
pnpm test
pnpm typecheck
pnpm build
pnpm format:check
```

The Postgres and S3 gates need a Postgres server whose URL may create databases
(`EARSHOT_TEST_POSTGRES_URL`) and an S3-compatible store (`EARSHOT_TEST_S3_URL`, for
example MinIO as in [storage and blobs](./storage-and-blobs.md#minio-for-local-testing)).
Run the storage modules listed in the `python-postgres` job of `.github/workflows/ci.yml`
with `--db postgresql`, then the blob-store suite, then enforce the coverage floor on the
combined data:

```bash
COVERAGE_FILE=.coverage.postgres pytest -q --db postgresql \
  --cov=earshot --cov-report= --cov-fail-under=0 <the modules in ci.yml>
COVERAGE_FILE=.coverage.postgres pytest -q \
  --cov=earshot --cov-append --cov-report= --cov-fail-under=0 apps/ingest/tests/test_blobstore.py
python -m coverage combine .coverage.sqlite .coverage.postgres
python -m coverage report
```

Then build and check the distributions:

```bash
DIST_DIR=$(mktemp -d /tmp/earshot-dist.XXXXXX)
python -m pip install 'build>=1.2,<2'
pnpm --filter @earshot/viewer bundle
python -m build --wheel --sdist --outdir "$DIST_DIR"
python scripts/check_wheel.py "$DIST_DIR"/*.whl
python scripts/check_sdist.py "$DIST_DIR"/*.tar.gz
python scripts/smoke_artifact.py "$DIST_DIR"/*.whl
python scripts/smoke_artifact.py "$DIST_DIR"/*.tar.gz
```

Each smoke command creates an isolated environment, installs the base artifact, proves
FastAPI and Uvicorn are absent, exercises the public SDK and CLI, installs the same
artifact with `[server]`, then starts the real server and fetches the API, viewer index,
and one compiled viewer asset. For an sdist this also proves that its isolated wheel
build retains the viewer.

Inspect the built metadata before upload:

```bash
unzip -p "$DIST_DIR"/earshot_observability-*.whl \
  'earshot_observability-*.dist-info/METADATA' | sed -n '1,40p'
```

The metadata `Name` must be `earshot-observability`; FastAPI, Uvicorn, SQLAlchemy and
Alembic must appear only behind extras including `server`, and psycopg and boto3 only
behind optional extras; and the wheel must contain the `earshot/` import directory, the
migration revisions, and the `earshot` console entry point.

## npm packages

`@earshot/browser`, `@earshot/viewer-ui` and `@earshot/node` are published from the same
tag, at the same version as the Python distribution. In the workspace, `@earshot/viewer-ui` exports its
TypeScript sources; its `publishConfig` points the published `exports` and `types` at
`dist/`. Only `pnpm pack` and `pnpm publish` apply `publishConfig`, so always pack it with
pnpm. Check the tarballs locally:

```bash
pnpm --filter @earshot/browser --filter @earshot/viewer-ui --filter @earshot/node build
pnpm --filter @earshot/browser --filter @earshot/viewer-ui --filter @earshot/node \
  pack --pack-destination "$DIST_DIR"
```

Each tarball must contain only `dist/`, `README.md`, `LICENSE` and `package.json`, every
`main`, `types` and `exports` target must exist, and no dependency may use a `workspace:`
range. The release workflow checks all three.

## Container image

The image installs `earshot-observability[server,postgres,s3,signals]` from wheels built in the
same Dockerfile, runs as a non-root user with `/data` as its only writable path, and checks
`/readyz`. It is about 142 MB, up from 94 MB before it gained the `signals` extra, which
analyses recordings for the call view. It carries OCI labels, with the version from the
`EARSHOT_VERSION` build argument:

```bash
docker build -t earshot:local --build-arg EARSHOT_VERSION=0.2.0-local .
sh scripts/smoke-container.sh
```

## Automated release

`.github/workflows/release.yml` is the only publication path. A manual workflow run is a
dry run: it builds and inspects the Python distributions and the npm tarballs but cannot
publish. A tag push publishes only when all of these identities agree:

- the tag is exactly `v<project.version>`;
- the tagged commit is contained in the repository's default branch;
- the Python distribution is `earshot-observability`;
- both npm packages have the tag's version; and
- the container identity is the lowercase `ghcr.io/<owner>/<repository>` path.

| Job                 | Runs on        | Does                                                                                                                                                  |
| ------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `validate`          | tag and manual | Checks the tag, version and image identity, and that the tag is on the default branch.                                                                |
| `build-python`      | tag and manual | Builds the viewer, wheel and sdist, and runs the wheel, sdist and smoke checks.                                                                       |
| `build-npm`         | tag and manual | Checks both package versions, builds and packs them with pnpm, and inspects the tarballs.                                                             |
| `publish-container` | tag            | Builds the Linux AMD64 image, pushes it to `ghcr.io/kkartik14/earshot` with SBOM and provenance (`mode=max`), and attests it.                         |
| `publish-pypi`      | tag            | Publishes the inspected wheel and sdist to PyPI with OIDC, after the image.                                                                           |
| `publish-npm`       | tag            | Publishes the inspected tarballs with `npm publish --provenance --access public`, after the image. A version containing `-` gets the `next` dist-tag. |
| `github-release`    | tag            | Attests the Python files and creates a GitHub Release with generated notes.                                                                           |

No long-lived PyPI or GHCR credential is stored in GitHub. npm uses trusted publishing
(OIDC) when it is configured; until then, the `NPM_TOKEN` secret is the fallback, and it
should be a short-lived token deleted after the first release.

Before the first tag, create the PyPI pending trusted publisher with these exact claims:

```text
PyPI project: earshot-observability
Owner:        Kkartik14
Repository:   Earshot
Workflow:     release.yml
Environment:  pypi
```

The GitHub `pypi` and `npm` environments restrict deployments to release tags and should
retain a required maintainer approval. Claim mismatches fail closed at PyPI. If the
repository, workflow, or environment is renamed, update PyPI and npm before the next
release.

[`release-checklist.md`](./release-checklist.md) is the owner's step-by-step list: the
one-time npm and GitHub environment setup, the four version fields to set, the
`CHANGELOG.md` entry, and the checks after publishing. Prepare a release on `main`, update
every intentionally coupled version, and let CI pass, including the `coverage` job. Then
create and push the matching tag:

```bash
git switch main
git pull --ff-only
python scripts/check_release.py --tag v0.2.0 --repository Kkartik14/Earshot
git tag -a v0.2.0 -m "Earshot 0.2.0"
git push origin v0.2.0
```

The first GHCR package may require one visibility check in the package settings. It must
be linked to this public repository and public before documenting anonymous `docker pull`
support. Released PyPI and npm versions are immutable, and a pushed image tag must not be
moved; never delete and recreate a version. Fix the problem, increment the package
version, and publish a new release.

## Alpha compatibility policy

`0.x` is a pre-v1 line, not a stable `1.x` contract. Compatibility is scoped to the
documented package, contract, semantic-profile, API, analyzer, adapter, Python, and
framework versions; one version number must not be used as a proxy for another.

- A patch release in a `0.y.x` line may add optional evidence or vocabulary but must
  continue accepting public inputs documented for earlier patches of the same line.
- 0.2.0 is such a minor release. Its breaking changes, including the one-way catalog
  upgrade and the new privacy defaults, are listed in [`CHANGELOG.md`](../CHANGELOG.md).
- Removing or repurposing a public import, accepted adapter input, persisted field,
  semantic code, or privacy default requires a declared deprecation or a new minor
  release with release notes and an explicit migration path.
- Producers emit contract and semantic-profile label `0.2.0`; the reader accepts `0.1.0`
  and `0.2.0`, and nothing else. Read tolerance ships with the bump, which is what makes
  a bump a migration rather than a break. No general support is claimed for artifacts
  labelled `1.0.0` or for any future version.
- Runtime compatibility is only the tested range below. Duck-typed unit fixtures do not
  extend a framework version claim.
- Mobile collectors, OTLP over gRPC, stereo recordings, presigned uploads, multiple
  servers per database, and other planned surfaces are outside the current compatibility
  contract.

The retained real-capture fixtures were regenerated on the current contract, semantic
profile, and adapter versions; they are not relabelled historical artifacts. Their
manifest pins the ignored source, checked-in driver, redactor, and public-artifact
digests. Stored user evidence must never be rewritten to simulate compatibility. Future
format support requires an explicit versioned decoder or normalizer.

## Supported versions and deprecation policy

This release tests CPython 3.11, 3.12, and 3.13. Linux is the primary full-suite target;
the compatibility workflow also runs a framework smoke on macOS. The `Requires-Python`
floor remains `>=3.11`; newer interpreters outside the tested matrix are unverified, not
prohibited. Pipecat support is `>=1.6.0,<1.7`, LiveKit Agents support is
`>=1.6.5,<1.7`, and standalone OpenTelemetry API/SDK support is `>=1.30,<2`. CI tests
the exact Pipecat/LiveKit lower bounds and the newest resolver-selected versions within
those ranges in separate jobs. Framework dependencies can impose a higher effective
OpenTelemetry floor than the standalone extra. A release must not claim compatibility
from a developer's single local environment alone.

The Pipecat minimum was raised from 1.5.0 to 1.6.0 in response to the
[upstream advisory](https://github.com/pipecat-ai/pipecat/security/advisories/GHSA-hqm2-cfx2-2vgh),
which marks versions through 1.5.0 as affected when the built-in development runner is
exposed beyond localhost. Pipecat 1.6.0 adds a warning and documents this boundary; it
does not add authentication. The runner remains unauthenticated by design and must not
be exposed to untrusted networks. Production deployments require an authenticated
dispatcher or managed runtime. Earshot's adapter does not start or expose the runner, and
its optional extras exclude the advisory's affected version range.

The optional Pipecat dependency graph still includes NLTK 3.10.3. Its
[upstream path-traversal advisory](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp)
has no patched release listed. The affected model-artifact path APIs are not called by
Earshot's adapter, but applications using those NLTK APIs must assess their exposure;
this transitive dependency means the optional Pipecat environment is not vulnerability-free.
Recheck the advisory before each release.

The Postgres catalog is tested on Postgres 16, and the S3 blob store against MinIO.

Windows is unverified and is not a supported durable-spool target. Directory
fsync, process creation, filesystem permissions, and service behavior require a dedicated
compatibility lane before that claim can change.

Dropping a tested Python version or raising a framework minimum is a public support
change. It must be announced in release notes and deprecated for at least one minor
release before removal; a security or upstream end-of-life exception must be called out
explicitly. Public imports, accepted adapter input shapes, persisted canonical bundles,
semantic codes, and privacy defaults must not be silently removed or repurposed. A
deprecated API emits `DeprecationWarning` with its replacement and planned removal
release, remains covered by compatibility tests during the window, and is removed only
in a declared breaking release. Newer readers must continue to decode supported
historical bundles; schema evolution uses an explicit version/normalizer rather than
rewriting stored evidence in place.
