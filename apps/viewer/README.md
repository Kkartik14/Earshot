# @earshot/viewer

The self-hostable web UI for Earshot — a session inspector for voice-AI
pipelines (turn timeline, per-span latency, call-graph, evidence, fleet metrics).

## Stack (and why)

A **static React SPA that is a pure client of the backend `/v1/*` API.** The same
built bundle is served by FastAPI in the single self-host container and by the
hosted app — it depends only on the API contract, so it is portable across every
deployment tier (embedded-local → single-container → team self-host → cloud).

This mirrors how the closest analogous tools ship: **Jaeger UI, Prometheus, and
Grafana** are all React SPAs served as static assets by their backend. (Langfuse
uses Next.js/SSR because it is hosted-first — not our local-first model.)

- **Vite + React + TypeScript** — build/dev tooling and framework
- **@tanstack/react-query** — server-state / data fetching
- **react-router-dom** — routing
- **openapi-typescript** — types generated from the backend's drift-checked
  `spec/backend-api.openapi.json`, so the UI stays in sync with the API (DRY)
- **Vitest + Testing Library** — unit/component tests
- Bespoke design tokens (no heavy component library) to keep the exact visual
  identity

## Develop

```bash
pnpm --filter @earshot/viewer dev        # dev server, proxies /v1 to the backend
pnpm --filter @earshot/viewer gen:api    # regenerate API types from the OpenAPI
pnpm --filter @earshot/viewer typecheck
pnpm --filter @earshot/viewer test
pnpm --filter @earshot/viewer build      # -> dist/, served by FastAPI in prod
```

Set `EARSHOT_API_URL` to point the dev proxy at a non-default backend
(default `http://127.0.0.1:8000`).

## Self-host (single process)

In production the API and the UI are one process — no separate web server. The
SPA calls `/v1/*` on its own origin, so FastAPI serves both:

```bash
pnpm --filter @earshot/viewer bundle     # build + copy dist -> the Python package's web/
earshot serve --data-dir .earshot        # then browse http://127.0.0.1:4319/
```

`bundle` drops the build into `packages/sdk-python/src/earshot/web/`, which
`earshot serve` serves by default (client routes fall back to `index.html`,
hashed assets are cached, and `/v1` is never shadowed). Point at an unbundled
build instead with `earshot serve --web-dir apps/viewer/dist` or
`EARSHOT_WEB_DIR`. With nothing bundled, the API just runs headless.

Serving the UI over a non-loopback address needs the auth story that ships with
the hosted tier; today this is a local, loopback self-host.
