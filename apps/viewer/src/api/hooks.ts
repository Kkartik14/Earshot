import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";

/** Recent incidents (one per stored voice session). */
export function useIncidents(
  query: { limit?: number; session_id?: string; cursor?: string } = {},
) {
  return useQuery({
    queryKey: ["incidents", query],
    queryFn: () => unwrap(api.GET("/v1/incidents", { params: { query } })),
  });
}

/** The full canonical incident for one session. */
export function useIncident(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["incident", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}", {
          params: { path: { bundle_id: bundleId as string } },
        }),
      ),
  });
}

/** The derived per-turn analysis (latency projections) for one session. */
export function useAnalysis(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["analysis", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/analysis", {
          params: { path: { bundle_id: bundleId as string } },
        }),
      ),
  });
}
