# `@earshot/viewer-ui`

Earshot's Observe feature, API client, generated API types, and incident views.
The package is framework neutral: hosts provide route IDs and a link component;
it does not depend on React Router or Next.js.

## Availability

This package is private and is not published. Earshot's Vite and Next.js hosts
consume it through the repository workspace. During local Platform integration,
link this package from the `Earshot-task3` checkout in Platform's workspace; do
not depend on a registry release. Publishing is a separate release step.

The package expects the host's React Query provider to wrap the feature. React
and React Query are peer dependencies so the host and feature share the same
React instance and query cache. `openapi-fetch` is installed as a package
dependency.

## Mount the Observe feature

For standalone viewer hosts, import `styles/tokens.css` and `styles/global.css`.
For an embedded host, import only `styles/embedded.css`; its tokens are scoped
to the Observe feature. Do not import the standalone styles into the shared
document because they change `html`, `body`, anchors, focus rules, and
root-level custom properties.

Render `ObserveFeature` inside the host's authenticated project shell:

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
        route={{ kind: "session", sessionId: "session-id-from-platform" }}
        currentPath="/observe"
        LinkComponent={HostLink}
        embedded
      />
    </ViewerQueryScopeProvider>
  );
}
```

For Next.js, add `@earshot/viewer-ui` to `transpilePackages`. Keep the API on a
same-origin path and configure the corresponding base path with
`configureApiBasePath`. The Platform app can resolve `/observe?sessionId=...`
to the `session` route above. The resolver uses the authenticated project's
incident and live-session APIs; when more than one artifact shares a session ID,
it asks the user to choose by immutable bundle ID. The chooser links to
`/sessions/{bundleId}`, so the host must map that path back to
`ObserveFeature` with `route={{ kind: "incident", bundleId }}` as well as mapping
the session route.

The standalone fleet empty state suggests ingesting a voice session. An embedded
host defaults to neutral “no turn metrics” guidance because the host may not
provide an ingestion action. Set `emptyFleetHint` on `ObserveFeature` when the
host has a supported, user-accessible way to populate Earshot.

The host owns user authentication and project authorization. Do not send
Platform service tokens, project API keys, provider credentials, or artifact
content to the browser. `projectId` is the canonical selected project ID.
`authContextId` is a nonsecret opaque cache namespace that the host rotates when
the user, project, or effective authorization changes. Never pass a bearer,
cookie, CSRF token, or other credential as the cache namespace.

All Observe queries include both scope values in their keys, pass cancellation
signals to fetch, and remove their scoped cache on scope change or unmount. The
cleanup removes only keys under the reserved `earshot` prefix; it leaves the
host's other React Query data intact. On logout, either unmount the provider or
call `clearViewerQueries(queryClient)` to remove all Earshot-owned entries while
preserving host queries. The host should unmount the provider when its
authenticated shell closes.

## Development

The standalone Vite and Next.js hosts in Earshot use this workspace package.
API types are generated from the backend OpenAPI artifact:

```bash
pnpm --filter @earshot/viewer-ui gen:api
pnpm --filter @earshot/viewer-ui typecheck
pnpm --filter @earshot/viewer-ui test
```

The package stays private while the Platform integration is developed. A
separate release can make it installable from a registry after its public API,
versioning, and release checks are ready.
