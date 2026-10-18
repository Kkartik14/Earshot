"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { configureApiBasePath, shouldRetryQuery } from "@earshot/viewer-ui";

const apiBasePath = process.env.NEXT_PUBLIC_EARSHOT_API_BASE_PATH ?? "";
configureApiBasePath(apiBasePath);

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: shouldRetryQuery,
          },
        },
      }),
  );

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
