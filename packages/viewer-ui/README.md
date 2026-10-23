# `@earshot/viewer-ui`

Earshot's Observe feature (React views for calls and incidents), a typed client
for the Earshot API, and the generated API types. It is framework neutral: the
host provides route IDs and a link component, and the package depends on neither
React Router nor Next.js.

## Install

```bash
npm install @earshot/viewer-ui react @tanstack/react-query
```

React and React Query are peer dependencies, so the host and the feature share
one React instance and one query cache. The package version matches the Earshot
server release whose API it was generated from.

The package ships ES modules, type declarations and CSS Modules. Use a bundler
that handles CSS Modules (Vite, webpack, Next.js). With Next.js, add
`@earshot/viewer-ui` to `transpilePackages`.

## Mount the Observe feature

For a standalone viewer, import `styles/tokens.css` and `styles/global.css`. For
an embedded host, import only `styles/embedded.css`; its tokens are scoped to
the feature. Do not import the standalone styles into a shared document: they
change `html`, `body`, anchors, focus rules and root-level custom properties.

Render `ObserveFeature` inside the host's authenticated project shell, under the
host's React Query provider:

```tsx
import "@earshot/viewer-ui/styles/embedded.css";
import {
  configureApiBasePath,
  ObserveFeature,
  ViewerQueryScopeProvider,
} from "@earshot/viewer-ui";
import type { ViewerLinkProps } from "@earshot/viewer-ui";

configureApiBasePath("/api/earshot");

function HostLink({ href, ...props }: ViewerLinkProps) {
  return <a href={href} {...props} />;
}

export function ObservePage() {
  return (
    <ViewerQueryScopeProvider
      scope={{ projectId: "canonical-project-id", authContextId: "host-auth-context-id" }}
    >
      <ObserveFeature
        route={{ kind: "session", sessionId: "session-id" }}
        currentPath="/observe"
        LinkComponent={HostLink}
        embedded
      />
    </ViewerQueryScopeProvider>
  );
}
```

- Keep the Earshot API on a same-origin path and pass that path to
  `configureApiBasePath`.
- When several incidents share a session ID, the session route asks the user to
  choose one and links to `/sessions/{bundleId}`. Map that path back to
  `ObserveFeature` with `route={{ kind: "incident", bundleId }}`.
- An embedded fleet view shows neutral empty-state guidance. Set
  `emptyFleetHint` when the host offers a way to send data to Earshot.
- When the host knows a call of its own but Earshot has no incident for it yet,
  render `route={{ kind: "capture-status", hostCallId, capture }}`. `capture` is
  the state the host reports: `waiting`, `delayed`, `missing`, or `rejected` with
  a `reasonCode`. The page labels it as host-reported and shows no evidence.
  `CaptureStatus` renders the same view outside `ObserveFeature`.

## Security and cache scope

The host owns user authentication and project authorization. Do not send
service tokens, project API keys, provider credentials or artifact content to
the browser.

`projectId` is the canonical selected project ID. `authContextId` is a
non-secret, opaque cache namespace that the host rotates when the user, project
or effective authorization changes; never pass a bearer token, cookie, CSRF
token or other credential as the namespace. Every Observe query key carries both
values under the reserved `earshot` prefix, and the provider removes its scoped
cache on scope change or unmount. On logout, unmount the provider or call
`clearViewerQueries(queryClient)`; the host's other queries are left intact.

## Development

Inside the Earshot repository the package exports its TypeScript sources, so the
Vite and Next.js viewers use them directly. `pnpm pack` swaps in the compiled
`dist` entry points from `publishConfig`, so build and pack with pnpm:

```bash
pnpm --filter @earshot/viewer-ui gen:api     # regenerate API types from spec/
pnpm --filter @earshot/viewer-ui typecheck
pnpm --filter @earshot/viewer-ui test
pnpm --filter @earshot/viewer-ui build       # tsc -> dist, plus CSS and API types
```

Licensed under Apache-2.0.
