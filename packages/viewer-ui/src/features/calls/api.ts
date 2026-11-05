import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import type { components, paths } from "../../api/schema";
import {
  api,
  ApiError,
  apiUrl,
  notifyViewerSessionInvalid,
  unwrap,
} from "../../api/client";
import { useViewerQueryScope, viewerQueryKey } from "../../api/queryScope";
import { parsePeaksDat, type Peaks } from "./peaks";

type Schemas = components["schemas"];
export type CallSummary = Schemas["CallSummaryResponse"];
export type CallPage = Schemas["CallPageResponse"];
export type CallDetail = Schemas["CallDetailResponse"];
export type CallLiveTurns = Schemas["CallLiveTurnsResponse"];
export type CallLiveTurnsCache = { body: CallLiveTurns; etag: string | null };
export type CallLiveTurnsView = CallLiveTurns & { etag: string | null };
export type CallProjection = Schemas["CallProjectionDetailResponse"];
export type CallMedia = Schemas["CallMediaResponse"];
export type CallTurn = Schemas["CallTurnResponse"];
export type TurnSpan = Schemas["TurnSpanResponse"];
export type AudibleGap = Schemas["AudibleGapResponse"];
export type CallCost = Schemas["CallCostResponse"];
export type CostItem = Schemas["CostItemResponse"];
export type CallSignals = Schemas["CallSignalsResponse"];
export type PlacedLine = Schemas["PlacedTranscriptLineResponse"];
export type SpanLine = Schemas["TranscriptLineResponse"];
export type CallSpan = Schemas["CallSpanResponse"];
export type SpanAttribute = Schemas["SpanAttributeResponse"];
export type SpanRef = Schemas["SpanRefResponse"];

/** Retry transient API/network failures; permanent client errors need a state change. */
export function isRetryableCallApiError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

/** The list's filters: every `GET /v1/calls` parameter except paging. */
export type CallListFilters = Omit<
  NonNullable<paths["/v1/calls"]["get"]["parameters"]["query"]>,
  "cursor" | "limit" | "state"
>;

const PAGE_SIZE = 50;

/** Calls, newest first evidence first, a page at a time ("load more"). */
export function useCalls(filters: CallListFilters) {
  const scope = useViewerQueryScope();
  return useInfiniteQuery<
    CallPage,
    Error,
    InfiniteData<CallPage>,
    readonly unknown[],
    string | undefined
  >({
    queryKey: viewerQueryKey(scope, ["calls", filters]),
    initialPageParam: undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/v1/calls", {
          params: {
            query: {
              ...filters,
              limit: PAGE_SIZE,
              ...(pageParam == null ? {} : { cursor: pageParam }),
            },
          },
          signal,
        }),
      ),
  });
}

/** One call with its projection (null until projected) and its span-transcript fields. */
export function useCall(callId: string) {
  const scope = useViewerQueryScope();
  return useQuery({
    queryKey: viewerQueryKey(scope, ["call", callId]),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/calls/{call_id}", {
          params: { path: { call_id: callId } },
          signal,
        }),
      ),
    retry: (failureCount, error) => failureCount < 3 && isRetryableCallApiError(error),
    refetchInterval: (query) => {
      if (!isRetryableCallApiError(query.state.error)) return false;
      return query.state.data?.state === "closed" &&
        query.state.data.latest_revision != null &&
        query.state.data.projection_state !== "current"
        ? 2_000
        : false;
    },
  });
}

/** Current turns and the latest Revision, revalidated while the call view is mounted. */
export function useLiveCallTurns(
  callId: string,
  detailState: CallDetail["state"] | undefined,
) {
  const scope = useViewerQueryScope();
  const queryClient = useQueryClient();
  const queryKey = viewerQueryKey(scope, ["live-call-turns", callId]);
  return useQuery({
    queryKey,
    queryFn: async ({ signal }): Promise<CallLiveTurnsCache> => {
      const previous = queryClient.getQueryData<CallLiveTurnsCache>(queryKey);
      const path =
        `/v1/calls/${encodeURIComponent(callId)}/live-turns` as `/v1/${string}`;
      const response = await fetch(apiUrl(path), {
        credentials: "same-origin",
        headers: previous?.etag == null ? undefined : { "If-None-Match": previous.etag },
        signal,
      });
      if (response.status === 304) {
        if (previous == null) {
          throw new ApiError(304, "EARSHOT_LIVE_TURNS_CACHE_MISSING");
        }
        return previous;
      }
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          error?: { code?: string };
        } | null;
        if (response.status === 401) notifyViewerSessionInvalid();
        throw new ApiError(
          response.status,
          body?.error?.code ?? `HTTP ${response.status}`,
        );
      }
      return {
        body: (await response.json()) as CallLiveTurns,
        etag: response.headers.get("ETag"),
      };
    },
    select: (cached): CallLiveTurnsView => ({ ...cached.body, etag: cached.etag }),
    retry: (failureCount, error) => failureCount < 3 && isRetryableCallApiError(error),
    refetchInterval: (query) => {
      if (!isRetryableCallApiError(query.state.error)) return false;
      if (query.state.data?.body.state === "closed") return false;
      return query.state.data != null || detailState != null ? 2_000 : false;
    },
  });
}

/** One stored span of the call, fetched only when a drawer asks for it. */
export function useCallSpan(callId: string, span: SpanRef | null) {
  const scope = useViewerQueryScope();
  return useQuery({
    queryKey: viewerQueryKey(scope, ["call-span", callId, span?.trace_id, span?.span_id]),
    enabled: span != null,
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/calls/{call_id}/spans/{trace_id}/{span_id}", {
          params: {
            path: {
              call_id: callId,
              trace_id: (span as SpanRef).trace_id,
              span_id: (span as SpanRef).span_id,
            },
          },
          signal,
        }),
      ),
  });
}

/** A server-relative path from a response body (`content_path`, `peaks_path`) as a
 * same-origin URL. The UI never builds media URLs itself; anything that is not an
 * API path is refused rather than followed. */
export function mediaUrl(path: string): string | null {
  return path.startsWith("/v1/") ? apiUrl(path as `/v1/${string}`) : null;
}

/** The waveform peaks a media entry names, parsed. Null `path`: none stored yet. */
export function usePeaks(path: string | null) {
  const scope = useViewerQueryScope();
  return useQuery<Peaks, Error>({
    queryKey: viewerQueryKey(scope, ["peaks", path]),
    enabled: path != null,
    // Peaks of a stored analysis never change; the server revalidates by ETag.
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const url = mediaUrl(path as string);
      if (url == null) throw new ApiError(0, "EARSHOT_PEAKS_PATH_INVALID");
      const response = await fetch(url, { credentials: "same-origin", signal });
      if (!response.ok) {
        if (response.status === 401) notifyViewerSessionInvalid();
        const body = (await response.json().catch(() => null)) as {
          error?: { code?: string };
        } | null;
        throw new ApiError(
          response.status,
          body?.error?.code ?? `HTTP ${response.status}`,
        );
      }
      return parsePeaksDat(await response.arrayBuffer());
    },
  });
}
