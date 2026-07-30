---
name: earshot-dev
description: Run Earshot's local day-to-day dev loop — the backend API and the hot-reloading viewer together, correctly wired to each other. Use when the user wants to run the app, start the dev server, see a UI change live, or test a change against the real backend/viewer. Assumes earshot-setup has already been run once.
---

# Earshot Dev Loop

Two independent processes make up the dev loop. Run both when working on viewer code;
run only the backend when working on Python/SDK/API code and testing via `curl`/CLI.

## Backend (FastAPI + CLI), port 4319 by default

```bash
earshot serve --data-dir .earshot
# or, from repo root: pnpm serve   (same command, defined in package.json)
```

Serves the API at `http://127.0.0.1:4319`. If `packages/sdk-python/src/earshot/web/`
has a bundled viewer in it (from a prior `pnpm --filter @earshot/viewer bundle`), this
also serves the built (non-hot-reloading) UI at `/`. Data persists under `.earshot/`
(SQLite + content-addressed objects) — safe to delete that directory for a clean slate.

Useful flags: `--port N` to change the port, `--log-level debug` for verbose logs,
`--checkpoint-dir DIR` to also expose in-progress/crash-recovery sessions under
`/v1/live`.

## Viewer dev server (hot-reload), proxies to the backend

```bash
pnpm --filter @earshot/viewer dev
```

**Port gotcha, verified real — read before running this:** the viewer's Vite dev
proxy (`apps/viewer/vite.config.ts`) defaults its API target to
`http://127.0.0.1:8000`, but the backend's own CLI default port is **4319**, not 8000.
If you start the backend with plain `earshot serve --data-dir .earshot` and then run
`pnpm --filter @earshot/viewer dev` with no further config, the viewer's `/v1` calls
will hit a backend that isn't there. Fix it one of two ways:

```bash
# Option A — point the dev proxy at the backend's real port:
EARSHOT_API_URL=http://127.0.0.1:4319 pnpm --filter @earshot/viewer dev

# Option B — run the backend on the port the proxy already expects:
earshot serve --data-dir .earshot --port 8000
```

Either is fine; pick one and keep both processes' port assumptions consistent for the
session. The viewer dev server itself listens on Vite's default port (5173) and opens
in a browser; that's what you actually browse to, not 4319/8000 directly.

## Everything in one process (no hot reload)

If you don't need live UI iteration, skip the two-process setup:

```bash
pnpm --filter @earshot/viewer bundle    # build once, copies into the Python package
earshot serve --data-dir .earshot        # -> http://127.0.0.1:4319 serves API + UI
```

Re-run `bundle` after every viewer source change; there's no watch mode for this path.

## Feeding it data to look at

```bash
curl -X POST http://127.0.0.1:4319/v1/incidents \
  -H 'Content-Type: application/json' --data-binary @fixtures/valid/minimal.json
curl -X POST http://127.0.0.1:4319/v1/incidents \
  -H 'Content-Type: application/json' --data-binary @fixtures/valid/complete.json
# or any of fixtures/faults/*.incident.json for a specific diagnosis scenario
```

Then open the viewer (Vite dev URL, or `http://127.0.0.1:4319/` for the bundled path)
and it should list the ingested session(s).

## Stopping cleanly

`Ctrl-C` both processes. Nothing needs a teardown step — `.earshot/` is just a local
data directory, safe to leave in place or delete between sessions.
