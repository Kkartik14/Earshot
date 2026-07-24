import { useQuery } from "@tanstack/react-query";
import type { GroupBy, MetricKey } from "../features/fleet/fleet";
import type { paths } from "./schema";
import { api, unwrap } from "./client";

/** The registered exporter names `GET /v1/incidents/{id}/export` accepts, taken
 * from the generated schema's `format` enum so a spec change that drops one of
 * them fails the build here rather than at request time. */
export type ExportFormat = NonNullable<
  NonNullable<
    paths["/v1/incidents/{bundle_id}/export"]["get"]["parameters"]["query"]
  >["format"]
>;

/** Recent incidents (one per stored voice session). `enabled` lets a caller that
 * only needs the list on demand (e.g. the known-good picker) defer the fetch
 * until it is opened, instead of loading it on every mount. */
export function useIncidents(
  query: { limit?: number; session_id?: string; cursor?: string } = {},
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: ["incidents", query],
    enabled,
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

/** The explicit "what the evidence does NOT tell us" for one incident: coverage
 * gaps, analysis/turn limitations, and policy omissions, each with its reason.
 * Kept as its own query so an unavailable or stale-analysis projection surfaces
 * as its coded state, and an empty answer reads as an examined "no gaps found"
 * — never silently collapses into "nothing is missing". */
export function useNotObserved(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["not-observed", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/evidence/not_observed", {
          params: { path: { bundle_id: bundleId as string } },
        }),
      ),
  });
}

/** The compact, agent-facing digest of one incident — session-level counts, its
 * diagnoses, and the earliest boundary — bound to the incident's analysis. Used
 * to frame the not-observed surface; it is never read as a substitute for the
 * per-turn analysis, which `/analysis` and `/explanation` serve. */
export function useEvidenceSummary(bundleId: string | undefined) {
  return useQuery({
    queryKey: ["evidence-summary", bundleId],
    enabled: bundleId != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/evidence/summary", {
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

/** A structured diff of this incident against a chosen known-good incident.
 *
 * Lazily enabled: it runs only once a baseline is picked, and never against the
 * incident itself. The baseline side keeps its own stable error codes
 * (`EARSHOT_KNOWN_GOOD_*`) so a failure names which incident is missing rather
 * than blanking the whole diff; those, and a stale-analysis conflict
 * (`EARSHOT_ANALYSIS_BINDING_MISMATCH`), surface as the code the viewer renders
 * as an explicit state. */
export function useComparison(
  bundleId: string | undefined,
  knownGoodBundleId: string | null,
) {
  return useQuery({
    queryKey: ["comparison", bundleId, knownGoodBundleId],
    enabled:
      bundleId != null && knownGoodBundleId != null && knownGoodBundleId !== bundleId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/comparison", {
          params: {
            path: { bundle_id: bundleId as string },
            query: { known_good_bundle_id: knownGoodBundleId as string },
          },
        }),
      ),
  });
}

/** Project this incident through a registered exporter, by name.
 *
 * Lazily enabled: it runs only once a format is requested. A policy refusal
 * (`EARSHOT_EXPORT_DENIED`) or an unregistered name
 * (`EARSHOT_UNKNOWN_EXPORT_FORMAT`) arrives as its stable code, which the viewer
 * renders as an explicit refusal rather than an empty or fabricated document. */
export function useExport(bundleId: string | undefined, format: ExportFormat | null) {
  return useQuery({
    queryKey: ["export", bundleId, format],
    enabled: bundleId != null && format != null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/incidents/{bundle_id}/export", {
          params: {
            path: { bundle_id: bundleId as string },
            query: { format: format as ExportFormat },
          },
        }),
      ),
  });
}
