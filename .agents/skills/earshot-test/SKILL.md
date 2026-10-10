---
name: earshot-test
description: Run Earshot's test suite (Python pytest across packages/sdk-python and apps/ingest, plus the viewer's Vitest suite) with the right default flags, marker filters, coverage, and lint. Use when the user wants to run tests, check coverage, lint, or verify a change didn't break anything before committing.
---

# Earshot Test Runner

Two independent suites — Python and TypeScript — plus lint/format. All commands below
assume `earshot-setup` has already been run (active `.venv`, `pnpm install` done).

## Full Python suite

```bash
python -m pytest -q
# or from repo root without activating the venv: pnpm test:python
```

~1,860 tests across `packages/sdk-python/tests` (unit/property, the larger and more
rigorously-tested half — durability, privacy, adapters, analysis) and
`apps/ingest/tests` (integration/e2e/security, against the real ASGI app). Expect one
skip: `test_real_provider_deliveries.py` skips until a real captured connector webhook
fixture exists under `apps/ingest/tests/fixtures/connectors/<provider>/` — that's
expected, not a failure.

### Running a subset by marker

`pyproject.toml` defines four markers: `unit`, `integration`, `e2e`, `property`.

```bash
python -m pytest -q -m unit                 # fast, isolated contract/analysis tests
python -m pytest -q -m "not e2e"            # skip the slow real-server-process tests
python -m pytest -q -m property             # hypothesis-based generative tests only
```

### Running one area

```bash
python -m pytest -q packages/sdk-python/tests/test_checkpoint_writer.py
python -m pytest -q packages/sdk-python/tests/test_analysis.py -k "diagnosis"
python -m pytest -q apps/ingest/tests/test_container_security.py
```

### Coverage

```bash
python -m pytest --cov=earshot --cov-report=term-missing
```

`pyproject.toml` sets `fail_under = 85` — a coverage run below that fails even if every
individual test passed. `branch = true` is enabled, so partial-branch lines show up in
`term-missing` too.

## Viewer (TypeScript) suite

```bash
pnpm --filter @earshot/viewer test          # vitest run, one-shot
pnpm --filter @earshot/viewer test:watch    # vitest watch mode, for active UI work
pnpm --filter @earshot/viewer typecheck     # tsc -b, no emit
```

Other workspace packages (`packages/browser`, `packages/schema`, `packages/analysis`,
`examples/browser-voice`) have their own test scripts too — run `pnpm test` from the
repo root to run all of them via Turborepo (`turbo run test`), or scope to one with
`pnpm --filter <package-name> test`.

## Lint and format

```bash
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples
ruff format --check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples
pnpm format:check   # prettier --check over **/*.{ts,tsx,md,json,yaml}
```

`ruff check` is also runnable via the root script `pnpm lint:python` (same target
paths). `ruff format --check` and `pnpm format:check` are formatting-only — use
`ruff format` / `pnpm format` (no `:check`) to actually rewrite files.

## Everything at once (closest to what CI runs)

```bash
pnpm test:all    # -> pnpm test (turbo run test, all TS packages) && pnpm test:python
```

This does **not** include `ruff check`/`ruff format --check`/`pnpm format:check`, or the
contract/fixture-generator drift checks (`generate_contract.py --check`,
`generate_openapi.py --check`, `generate_fault_fixtures.py --check`,
`check_semconv.py`) that `.github/workflows/ci.yml`'s `python` job also runs. Run those
generator `--check` scripts too before treating a change to `contract.py`, `api.py`,
`semconv/earshot.yaml`, or `fixtures/faults/` as done — a drifted generated artifact
passes `pytest` locally but fails CI.

## Before considering a change finished

1. `python -m pytest -q` (or a scoped subset if the full run is slow for the task).
2. `pnpm --filter @earshot/viewer test` if any TS/viewer file changed.
3. `ruff check <touched python paths>` and `pnpm format:check` if any file changed.
4. If `contract.py`/`api.py`/`semconv/earshot.yaml`/`fixtures/faults/` changed, re-run
   the matching `scripts/generate_*.py --check` (or without `--check` to update, then
   review the diff) so `spec/` and the viewer's generated fixtures don't drift.
