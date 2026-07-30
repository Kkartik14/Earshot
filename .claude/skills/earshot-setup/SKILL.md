---
name: earshot-setup
description: Bootstrap a fresh clone of Earshot for local development — Python venv, editable install, pnpm workspace install, viewer bundle — then verify the server actually runs. Use when the user wants to set up, install, or bootstrap the Earshot dev environment for the first time, is onboarding to this repo, or says the project "isn't set up" / "won't run" / "pip install fails" / "pnpm install fails".
---

# Earshot Setup

Bootstraps the "from source" path documented in the root `README.md`. Every command
below has been hand-verified end to end (venv → pip install → pytest collection →
pnpm install → viewer bundle → server → healthz/readyz/incident-post) — follow it in
order rather than improvising, and don't skip the verification step at the end.

## 0. Check prerequisites first

- **Python 3.11+** (`pyproject.toml` requires `>=3.11`; 3.12/3.13 also supported).
  `python3.11 --version`
- **Node >= 20** (`.nvmrc` pins `20`, `package.json` `engines` requires `>=20.0.0`).
  `node --version`
- **pnpm** — `package.json` pins `packageManager: pnpm@10.34.5`. If a different pnpm
  is active, `corepack` or `npm i -g pnpm@10.34.5` before continuing; a much newer/older
  pnpm can resolve the lockfile differently.
- **Docker** (optional) — only needed for the container path in step 6.

If any of these are missing, install them before proceeding rather than working around
their absence — this project's CI matrix and lockfile assume these exact bounds.

## 1. Python virtual environment

From the repo root:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
```

Skip creating a new one if `.venv/` already exists and `.venv/bin/python --version`
reports 3.11.x — just activate it.

## 2. Editable install with dev extras

```bash
pip install -e '.[dev]'
```

This installs `earshot-observability` in editable mode plus the `dev` extra
(`pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`, `fastapi`, `uvicorn`,
`httpx`, `cryptography`, `pyyaml`, `grpcio-tools`, `build`). Verify it landed correctly:

```bash
pip show earshot-observability   # "Editable project location" should be this repo root
```

Applications that only *emit* Earshot evidence (no local server/CLI) only need the
lightweight base package (`pip install earshot-observability`, no extras) — the `dev`
extra above is for working on Earshot itself. Running the server from a
non-development install additionally needs the `server` extra:
`pip install 'earshot-observability[server]'`.

Optional extras exist for instrumenting real frameworks/providers: `pipecat`,
`pipecat-groq`, `livekit`, `otel`, `spool-encryption` — only add these if the task
actually touches that integration.

## 3. Sanity-check the Python install

```bash
python -m pytest --collect-only -q
```

Should report roughly 1800+ tests collected across `packages/sdk-python/tests` and
`apps/ingest/tests` with no collection errors. If this fails, the venv/install is
broken — fix it before moving on; don't proceed to pnpm with a broken Python side.

## 4. pnpm workspace install

```bash
pnpm install
```

Workspace covers `packages/*`, `apps/*`, and any `examples/*` subdir that ships its own
`package.json` (currently `examples/browser-voice`). If the lockfile is already
satisfied this is a fast no-op ("Already up to date").

## 5. Build the viewer into the Python package

```bash
pnpm --filter @earshot/viewer bundle
```

This runs `tsc -b && vite build`, then copies `apps/viewer/dist` into
`packages/sdk-python/src/earshot/web/` (gitignored — it's build output, never hand-edit
it or expect it in `git status`). Without this step the API still runs fine; it just
serves no UI at `/`.

## 6. Verify the server actually works

Pick ONE of these two paths and confirm the exact same checks pass.

**From source:**

```bash
earshot serve --data-dir .earshot   # http://127.0.0.1:4319
```

**Or the container path** (needs Docker running):

```bash
docker compose up --build   # http://127.0.0.1:4319, non-root, loopback-only by default
```

Either way, in a second terminal (or backgrounded), confirm:

```bash
curl -sS http://127.0.0.1:4319/healthz    # {"status":"ok"}
curl -sS http://127.0.0.1:4319/readyz     # {"status":"ready"}
curl -sS -X POST http://127.0.0.1:4319/v1/incidents \
  -H 'Content-Type: application/json' --data-binary @fixtures/valid/minimal.json
# should return a 201/200 JSON body with "bundle_id":"fixture-minimal", "status":"completed"
```

If any of these fail:
- `curl: couldn't connect` → the server isn't actually listening; check its stdout/logs.
- A `500`/`ArtifactCorruptionError` crash loop when reusing an **existing** data
  dir/Docker volume from a much older checkout usually means the on-disk schema
  predates recent storage changes. Confirm by trying a clean data dir/volume first:
  `earshot serve --data-dir /tmp/earshot-check` (from source) or
  `docker compose down -v && docker compose up --build` (container — this destroys the
  named volume, so only do it if the existing data isn't needed). If a clean dir/volume
  works but the old one doesn't, that's a genuine local-state incompatibility, not a
  setup failure — don't "fix" it by silently wiping the user's real data volume without
  asking first.

Stop the server (`Ctrl-C`, or `docker compose down` for the container) once verified.

## 7. What's next

- Day-to-day dev loop (hot-reloading viewer + backend): use the `earshot-dev` skill.
- Running the test suite: use the `earshot-test` skill.
- Full architecture map: `docs/README.md`. Vocabulary: `CONTEXT.md`.
