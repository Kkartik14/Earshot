# Earshot Vite host

This small React Router host keeps the local, static Earshot viewer path. Its
Observe screens come from the shared Earshot-owned `@earshot/viewer-ui`
workspace package, so the host owns only routing, standalone key exchange, and
the Vite build.

```bash
pnpm --filter @earshot/viewer dev
pnpm --filter @earshot/viewer typecheck
pnpm --filter @earshot/viewer test
pnpm --filter @earshot/viewer build
pnpm --filter @earshot/viewer bundle
```

The Vite dev server proxies `/v1`, `/healthz`, and `/readyz` to
`EARSHOT_API_URL` (default `http://127.0.0.1:8000`). `bundle` copies the static
build into the Python package so `earshot serve` and the single-process Docker
image continue to include the self-hosted viewer.

Use `apps/viewer-next` for the separately runnable Next.js host. It imports the
same Observe feature package and provides App Router navigation.
