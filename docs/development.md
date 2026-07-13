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
pip install -e '.[livekit]'
pip install -e '.[otel]'
```

Importing `earshot` must work without either runtime installed.

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
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples/livekit_console
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

For remote access, terminate HTTPS in a same-host proxy that connects to Earshot's
loopback socket, then set a bearer token and `--behind-tls-proxy`. M1 rejects a
non-loopback listener instead of trusting forwarded headers.

CLI workflows:

```bash
earshot validate fixtures/valid/minimal.json
earshot ingest fixtures/valid/minimal.json --data-dir .earshot
earshot list --data-dir .earshot
earshot show <bundle-id> --data-dir .earshot --format json
earshot purge <bundle-id> --data-dir .earshot
```

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
- The server is local/single-node and does not implement multi-tenant authorization.
- Media upload/replay, browser collection, additional provider-specific native S2S
  adapters, P.563 processing, and standard OTLP receiving are later milestones.
- Broad automatic failure explanation and incident-to-regression conversion are later
  milestones; the current analyzer provides deterministic projections and measured
  failed-operation diagnoses.
