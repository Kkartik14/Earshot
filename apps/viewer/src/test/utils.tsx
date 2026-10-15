import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { ViewerQueryScopeProvider } from "@earshot/viewer-ui";

/** Render a component inside the app's providers (fresh query client, router). */
export function renderWithProviders(
  ui: ReactElement,
  { route = "/" }: { route?: string } = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <ViewerQueryScopeProvider
          scope={{ projectId: "test-project", authContextId: "test-auth" }}
        >
          <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
        </ViewerQueryScopeProvider>
      </QueryClientProvider>
    );
  }
  return render(ui, { wrapper: Wrapper });
}
