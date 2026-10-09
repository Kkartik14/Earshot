import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

export interface ViewerQueryScope {
  /** Canonical project selected by the authenticated host. */
  projectId: string;
  /** Nonsecret opaque ID rotated whenever the host's authorization context changes. */
  authContextId: string;
}

const ViewerQueryScopeContext = createContext<ViewerQueryScope | null>(null);

export function viewerQueryScopePrefix(scope: ViewerQueryScope) {
  return ["earshot", scope.projectId, scope.authContextId] as const;
}

export function viewerQueryKey<T extends readonly unknown[]>(
  scope: ViewerQueryScope,
  resource: T,
) {
  return [...viewerQueryScopePrefix(scope), ...resource] as const;
}

/** Remove only Earshot-owned query data, preserving queries owned by the host. */
export function clearViewerQueries(queryClient: QueryClient): void {
  queryClient.removeQueries({ queryKey: ["earshot"] });
}

export function ViewerQueryScopeProvider({
  scope,
  children,
}: {
  scope: ViewerQueryScope;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();

  useEffect(() => {
    const prefix = viewerQueryScopePrefix(scope);
    return () => {
      queryClient.removeQueries({ queryKey: prefix });
    };
  }, [queryClient, scope.projectId, scope.authContextId]);

  return (
    <ViewerQueryScopeContext.Provider value={scope}>
      {children}
    </ViewerQueryScopeContext.Provider>
  );
}

export function useViewerQueryScope(): ViewerQueryScope {
  const scope = useContext(ViewerQueryScopeContext);
  if (scope == null) {
    throw new Error("ObserveFeature requires ViewerQueryScopeProvider");
  }
  return scope;
}
