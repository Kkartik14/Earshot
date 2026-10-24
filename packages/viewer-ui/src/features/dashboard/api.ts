import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { api, unwrap } from "../../api/client";
import { useViewerQueryScope, viewerQueryKey } from "../../api/queryScope";

type Schemas = components["schemas"];
export type DashboardBody = Schemas["DashboardResponse"];
export type DashboardTiles = Schemas["DashboardTilesResponse"];
export type LatencyTile = Schemas["DashboardLatencyResponse"];
export type GroupBy = DashboardBody["group_by"];

export interface DashboardRequest {
  from: string;
  to: string;
  bucket: DashboardBody["bucket"];
  groupBy: GroupBy;
}

/** The project's dashboard over one range (`GET /v1/dashboard`). The previous
 * range's numbers stay up while the next ones load. */
export function useDashboard({ from, to, bucket, groupBy }: DashboardRequest) {
  const scope = useViewerQueryScope();
  return useQuery({
    queryKey: viewerQueryKey(scope, ["dashboard", from, to, bucket, groupBy]),
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/dashboard", {
          params: { query: { from, to, bucket, group_by: groupBy } },
          signal,
        }),
      ),
  });
}
