"use client";

import { useParams } from "next/navigation";
import { ObserveRouteView } from "../../ObserveRoute";

export default function LivePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  return <ObserveRouteView route={{ kind: "live", sessionId }} />;
}
