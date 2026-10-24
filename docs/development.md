# Development and reproduction

## Prerequisites

- Python 3.11+
- Node 20+ only for the TypeScript viewer and browser packages
- `pnpm` for the TypeScript workspace

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Framework extras are deliberately optional. These `pip` installs are convenient local
setups, but `pip` does not consume `uv.lock`; use the locked instructions below for a
reproducible provider-matrix run:

```bash
pip install -e '.[pipecat]'
pip install -e '.[pipecat-groq]'
pip install -e '.[pipecat-matrix]'
pip install -e '.[livekit]'
pip install -e '.[otel]'
```

Importing `earshot` must work without either runtime installed.
The provider-neutral `pipecat` extra installs tracing only. The `pipecat-groq` extra
adds the Groq service dependency used by the checked-in real STT, LLM, and TTS driver.
The `pipecat-matrix` extra pins Pipecat 1.6.0 and installs the optional clients needed
to run every supported provider combination in `scripts/run_pipecat_matrix.py`.

Editable installs use the repository path, but built/published package metadata uses
the collision-free distribution name `earshot-observability`. See
[release packaging](./release.md); `pip install earshot` installs an unrelated VAD
project even though this repository also uses `import earshot`.

The compatibility lane checks Pipecat 1.6.0 and current 1.6.x, plus LiveKit Agents 1.6.5
and current 1.8.x. Unit tests remain duck typed; installing both extras activates the
real-package integration tests in `apps/ingest/tests/test_framework_integrations.py`.

## Generate contract artifacts

```bash
python scripts/generate_contract.py
python scripts/generate_contract.py --check
python scripts/generate_openapi.py
python scripts/generate_openapi.py --check
python scripts/generate_fault_fixtures.py
python scripts/generate_fault_fixtures.py --check
python scripts/generate_viewer_explanation_fixtures.py
python scripts/generate_viewer_explanation_fixtures.py --check
python scripts/generate_complete_fixture.py
python scripts/generate_canonical_vector.py
python scripts/check_semconv.py
```

The contract command compiles `proto/earshot/v1alpha1/incident.proto` and regenerates both
Pydantic JSON Schemas. The OpenAPI command regenerates the backend contract. Commit
generated outputs and use every `--check` command in CI to reject drift.

The fault-fixture generator produces one deterministic, semantically valid incident
for every plan scenario. The fixture test verifies both validity and required signals.
The corpus is pinned to the oldest contract and semantic-profile version the reader
still accepts, not to the version producers emit, so it also serves as read-side
backward-tolerance coverage. Regenerating it on a clean tree is therefore a no-op;
`--check` enforces that, in CI and in `pytest`.

## Run tests

```bash
pytest
pytest --cov=earshot --cov-report=term-missing
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples
pnpm test
pnpm typecheck
pnpm format:check
python scripts/generate_contract.py --check
python scripts/generate_openapi.py --check
python scripts/generate_fault_fixtures.py --check
python scripts/check_semconv.py
pnpm --filter @earshot/viewer bundle
pnpm --filter @earshot/viewer-next typecheck
pnpm --filter @earshot/viewer-next build
python -m build --wheel --sdist --outdir /tmp/earshot-dist
python scripts/check_wheel.py /tmp/earshot-dist/*.whl
python scripts/check_sdist.py /tmp/earshot-dist/*.tar.gz
python scripts/smoke_artifact.py /tmp/earshot-dist/*.whl
python scripts/smoke_artifact.py /tmp/earshot-dist/*.tar.gz
python scripts/viewer_smoke.py   # after the bundle step: the call views on replayed calls
```

`scripts/viewer_smoke.py` starts `earshot serve`, replays the two captured calls
(`fixtures/captured/pipecat-otlp-1.5`, `fixtures/captured/livekit-otlp-1.8`), waits for
the server to analyse and project them, and requests everything the viewer's calls pages
request: the pages and their scripts, the list with each filter, each call view, its
peaks, its audio by `Range`, and its spans. It needs only the standard library, so it can
drive an installed artifact: `python scripts/viewer_smoke.py --earshot
/path/to/venv/bin/earshot`. `--keep-serving` leaves the server up for a look in a
browser.

Plain `pytest` runs every test, including the ones marked `slow` (about 10 s or more
each: the 10,000-incident reprojection boot, the random-SIGKILL OTLP property test, the
1 GiB blob and recording memory tests, and the replay and real-provider quick-start e2e
tests). While you work, skip them and run only the slow tests of the modules you changed:

```bash
pytest -m "not slow"                                   # everything but the slow tests
pytest -m slow apps/ingest/tests/test_recordings_memory.py   # the slow tests you touched
pytest -m slow                                         # every slow test
```

Pull-request CI runs bounded feature suites and excludes tests marked `slow`. A
manually triggered `workflow_dispatch` run executes the full SQLite suite and the
Postgres/S3 storage suites with branch coverage, then combines their coverage results.
The release gate runs plain `pytest`, so no `slow` test is skipped there.

Two tests still skip in CI: the real-provider e2e (`apps/ingest/tests/e2e_real/`,
marked `real_providers`). Each runs one real call, Pipecat 1.5 or LiveKit Agents 1.8,
into Earshot on Postgres and S3, waits for the server to analyse and project it, and
checks its call view. They need `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`
and `CARTESIA_VOICE_ID` in the environment, the examples' `requirements.txt`, and the S3
test server. The LiveKit test also needs `EARSHOT_LIVEKIT_PYTHON`: the python of a
separate venv with `examples/livekit_otlp/requirements.txt` and Earshot installed
(LiveKit 1.8 needs OpenTelemetry versions Earshot's own lock cannot take). Without any of
them a test skips. They read keys from the process environment only, never from `.env`,
so a plain local `pytest` places no paid calls. The release gate runs them with
`EARSHOT_REQUIRE_REAL_PROVIDERS=1`, which turns a skip into a failure:

```bash
(set -a; . ./.env; set +a; EARSHOT_REQUIRE_REAL_PROVIDERS=1 \
  EARSHOT_LIVEKIT_PYTHON=/path/to/livekit-venv/bin/python pytest -q -m real_providers)
```

The provider-free replay of both calls
(`apps/ingest/tests/e2e/test_otlp_replay_quickstart.py`) runs in the full-coverage CI
workflow and pins their call views' values.

Tests intentionally mutate valid fixtures. A passing test should prove a property or
regression, not merely assert that its own valid sample is valid.

The checked-in [CI workflow](../.github/workflows/ci.yml) runs bounded feature gates on
pushes to `main` and on pull requests. Its Python lanes install both bounded framework
extras, so the LiveKit and Pipecat real-object integration tests cannot silently skip, and
its framework matrix exercises the supported minimum and current Pipecat and LiveKit
versions. Full SQLite and Postgres/S3 suites with combined branch coverage run only from
`workflow_dispatch`. A separate clean-artifact lane bundles the viewer, builds the wheel
and sdist together, and installs them in isolated base, `[server]`, and viewer-smoke
environments. Its TypeScript lane uses the committed pnpm lockfile and the Node version in
`.nvmrc`.

## Run locally

```bash
earshot serve --data-dir .earshot
curl http://127.0.0.1:4319/healthz
```

For remote access, terminate HTTPS in a trusted proxy and set `--behind-tls-proxy`.
Provision a project API key (preferred) or set the legacy default-project bearer token.
The remote viewer asks for that credential once and exchanges it for a bounded,
expiring server-side session. The browser receives only an HttpOnly, SameSite=Strict,
Secure cookie; the credential is not written to localStorage or sessionStorage. Signing
out revokes the session, and revoking its project API key invalidates it on the next
request.
Server startup prints the resolved active data path so operators can verify that the
intended persistent volume is in use without exposing host paths on the public health API.

CLI workflows:

```bash
earshot validate fixtures/valid/minimal.json
earshot ingest fixtures/valid/minimal.json --data-dir .earshot
earshot list --data-dir .earshot
earshot show <bundle-id> --data-dir .earshot --format json
earshot purge <bundle-id> --data-dir .earshot

earshot project create support --display-name "Support Voice" --data-dir .earshot
earshot api-key issue --project support --label production --data-dir .earshot
earshot connector create --project support --provider elevenlabs \
  --secret-env ELEVENLABS_WEBHOOK_SECRET --data-dir .earshot
```

The API-key credential is printed once; only its scrypt hash and salt are stored. A
Connector stores an environment-variable reference, never the provider secret.

## Run the single image

```bash
export EARSHOT_TOKEN="$(openssl rand -hex 32)"
docker compose up --build
curl http://127.0.0.1:4319/readyz
```

The named `earshot-data` volume owns `/data`. Recreating the container preserves data;
removing the named volume removes it. The container runs as an unprivileged user with a
read-only root filesystem, no Linux capabilities, and a small writable `/tmp` tmpfs.
The image itself defaults to container-loopback and does not assert trusted TLS proxying;
Compose explicitly opts into the container-network bind while publishing only on host
loopback. Do not copy that proxy assertion into a public port mapping without real TLS
termination.

For a public hostname, keep Compose bound to loopback and terminate TLS on the host:

```caddyfile
earshot.example.com {
  reverse_proxy 127.0.0.1:4319
}
```

Exercise the same build/auth path used by CI with `sh scripts/smoke-container.sh`. The
smoke ingests a canonical fixture, replaces the container while retaining the named
volume, and proves the fixture remains readable under the non-root/read-only hardening.

The roomless Pipecat smoke driver uses macOS `say` for its local microphone-side input
and Groq for STT, LLM, and TTS. Usage may be billed or rate-limited according to the
current Groq plan:

```bash
set -a && . ./.env && set +a
python examples/pipecat_headless/drive.py
```

It exits successfully only after a valid completed incident contains real STT, LLM,
and TTS operations plus TTS audio accepted by the discard output transport. This is
server-side output evidence; client render remains explicitly unobserved.

The full provider matrix uses the optional `pipecat-matrix` extra and real credentials
for each configured cell. It is a live, potentially billable run; timed-out or failed
cells are not converted into synthetic passes. See the dated
[36-cell results and limits](./pipecat-provider-matrix-2026-10-20.md) for the locked
setup and invocation commands before running it.

## Add an adapter

1. Consume native tracing/observer facts; do not create a second trace root.
2. Apply privacy filtering before queues/serialization.
3. Preserve native trace/span/parent/link identity and allowed source attributes.
4. Classify with `earshot.operation.name` without overwriting source span names.
5. Tag provenance and distinguish measured, estimated, and inferred facts.
6. Record unavailable signals in coverage; do not fake zero-duration stages.
7. Remain fail-open: adapter/export exceptions must not break the voice loop.
8. Add raw-source fixtures, semantic golden output, privacy sentinels, and an
   equivalence projection.

## Schema changes

- Wrapper breaking changes require a semantic-version bump.
- New open-vocabulary values do not require a wrapper change.
- Update proto, models, registry, docs, generated schema, and conformance fixtures
  together.
- Preserve original incoming protobuf bytes if unknown-field loss would violate a
  forward-compatible re-export promise.

## Operational limits

- Bundles are final post-session snapshots, not streaming assembly.
- Automatic capture is the normalized profile. Exact raw OTLP chunks are supported
  only through explicit caller-supplied, policy-enabled SDK input.
- The server is single-node; Project scoping is an authorization boundary, not a
  distributed multi-organization control plane.
- Browser capture (an unpublished `packages/browser` SDK plus its `/v1/capture`
  ingest endpoint) exists but is validated on one browser only, and the viewer does
  not yet render browser-captured sessions.
- Media upload/replay, render collection (confirming client playback actually
  occurred), native S2S adapters beyond the shipped OpenAI Realtime event mapper,
  P.563 processing, and generic live OTLP receiving are later milestones.
- Broad automatic failure explanation and incident-to-regression conversion are later
  milestones; the current analyzer provides deterministic projections and measured
  failed-operation diagnoses.
