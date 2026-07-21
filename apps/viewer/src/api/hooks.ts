import { useQuery } from "@tanstack/react-query";
import type { GroupBy, MetricKey } from "../features/fleet/fleet";
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

/** Fleet-wide turn-latency percentiles for one metric, grouped for comparison. */
export function useTurnMetrics(metric: MetricKey, groupBy: GroupBy) {
  return useQuery({
    queryKey: ["turn-metrics", metric, groupBy],
    queryFn: () =>
      unwrap(
        api.GET("/v1/metrics/turns", {
          params: { query: { metric, group_by: groupBy } },
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
