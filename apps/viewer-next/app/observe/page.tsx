import { ObserveRouteView } from "../ObserveRoute";

export default async function ObservePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const sessionId = params.sessionId;
  if (typeof sessionId !== "string" || sessionId.length === 0) {
    return <ObserveRouteView route={{ kind: "fleet" }} />;
  }
  return <ObserveRouteView route={{ kind: "session", sessionId }} />;
}
