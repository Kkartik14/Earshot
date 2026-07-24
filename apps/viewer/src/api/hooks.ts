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

/** Conversations still being written. Never incidents: they carry no artifact,
 * no digest, and therefore no analysis. The backend ships its own limitations
 * with the collection and the viewer renders them rather than paraphrasing. */
export function useLiveSessions() {
  return useQuery({
    queryKey: ["live-sessions"],
    refetchInterval: 5_000,
    queryFn: () => unwrap(api.GET("/v1/live/sessions")),
  });
}

/** Stored incidents for one session id, polled only while something is waiting
 * for one to appear. Used for the explicit hand-off from a live view to the
 * artifact — the live view is never silently upgraded in place. */
export function useSessionIncidents(
  sessionId: string | undefined,
  { enabled, pollMs }: { enabled: boolean; pollMs?: number },
) {
  return useQuery({
    queryKey: ["incidents", { session_id: sessionId }],
    enabled: enabled && sessionId != null,
    refetchInterval: pollMs ?? false,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents", {
          params: { query: { session_id: sessionId as string } },
        }),
      ),
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

/** Backend-detected contradictions in one incident's evidence graph. Kept as its
 * own query so a failure to detect them is visible as a failure, and never
 * collapses into an empty "no conflicts" reading of the session. */
export function useContradictions(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["contradictions", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/contradictions", {
          params: { path: { bundle_id: bundleId as string } },
        }),
      ),
  });
}

/** Backend-authored, evidence-bound timeline facts for one incident. */
export function useExplanation(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["explanation", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/explanation", {
          params: { path: { bundle_id: bundleId as string } },
        }),
      ),
  });
}
