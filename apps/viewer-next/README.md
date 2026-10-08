# Earshot Next.js host

This is a separately runnable App Router host for Earshot Observe. It imports
`@earshot/viewer-ui`; the Platform shell can consume that same feature without
using this standalone key-login shell.

```bash
pnpm install
EARSHOT_API_URL=http://127.0.0.1:4319 pnpm --filter @earshot/viewer-next dev
```

The Next server rewrites `/v1/*`, `/healthz`, and `/readyz` to `EARSHOT_API_URL`
(default `http://127.0.0.1:4319`). The browser stays on the Next origin, so the
project key exchange uses the Earshot HttpOnly session cookie. To mount the
upstream under a path prefix, configure the matching
`NEXT_PUBLIC_EARSHOT_API_BASE_PATH` on the Next host and its same-origin proxy.
Hosted Platform deployments should use a server-side proxy that authenticates
the Platform user and injects the server-held Earshot project credential.

The Python wheel and single-process Docker image still bundle the Vite host's
static build. They do not need a Node server at runtime.
