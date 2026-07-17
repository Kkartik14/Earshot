# Development and reproduction

## Prerequisites

- Python 3.11+
- Node 20+ only for the legacy TypeScript prototype/viewer work
- `pnpm` for existing TS packages

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Framework extras are deliberately optional:

```bash
pip install -e '.[pipecat]'
pip install -e '.[pipecat-groq]'
pip install -e '.[livekit]'
pip install -e '.[otel]'
```

Importing `earshot` must work without either runtime installed.
The provider-neutral `pipecat` extra installs tracing only. The `pipecat-groq` extra
adds the Groq service dependency used by the checked-in real STT, LLM, and TTS driver.

Editable installs use the repository path, but built/published package metadata uses
the collision-free distribution name `earshot-observability`. See
[release packaging](./release.md); `pip install earshot` installs an unrelated VAD
project even though this repository also uses `import earshot`.

The compatibility lane is bounded to Pipecat `>=1.5.0,<1.6` and LiveKit Agents
`>=1.6.5,<1.7`. Unit tests remain duck typed; installing both extras activates the
real-package integration tests in `apps/ingest/tests/test_framework_integrations.py`.

## Generate contract artifacts

```bash
python scripts/generate_contract.py
python scripts/generate_contract.py --check
python scripts/generate_openapi.py
python scripts/generate_openapi.py --check
python scripts/generate_fault_fixtures.py
python scripts/generate_complete_fixture.py
python scripts/generate_canonical_vector.py
python scripts/check_semconv.py
```

The contract command compiles `proto/earshot/v1/incident.proto` and regenerates both
Pydantic JSON Schemas. The OpenAPI command regenerates the backend contract. Commit
generated outputs and use both `--check` commands in CI to reject drift.

The fault-fixture generator produces one deterministic, semantically valid incident
for every plan scenario. The fixture test verifies both validity and required signals.

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
python scripts/check_semconv.py
python -m pip wheel --no-deps . --wheel-dir /tmp/earshot-dist
```

Tests intentionally mutate valid fixtures. A passing test should prove a property or
regression, not merely assert that its own valid sample is valid.

The checked-in [CI workflow](../.github/workflows/ci.yml) runs these gates on every
push and pull request. Its Python lane installs both bounded framework extras so the
LiveKit 1.6 and Pipecat 1.5 real-object integration tests cannot silently skip, then
builds the same PEP 517 wheel users install. Its TypeScript lane uses the committed
pnpm lockfile and the Node version in `.nvmrc`.

## Run locally

```bash
earshot serve --data-dir .earshot
curl http://127.0.0.1:4319/healthz
```

For remote access, terminate HTTPS in a trusted proxy and set `--behind-tls-proxy`.
Provision a project API key (preferred) or set the legacy default-project bearer token.
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

- M1 bundles are final post-session snapshots, not streaming assembly.
- M1 automatic capture is the normalized profile. Exact raw OTLP chunks are supported
  only through explicit caller-supplied, policy-enabled SDK input.
- The server is single-node; Project scoping is an authorization boundary, not a
  distributed multi-organization control plane.
- Media upload/replay, browser collection, additional provider-specific native S2S
  adapters, P.563 processing, and generic live OTLP receiving are later milestones.
- Broad automatic failure explanation and incident-to-regression conversion are later
  milestones; the current analyzer provides deterministic projections and measured
  failed-operation diagnoses.
