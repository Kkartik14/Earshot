"use client";

import { useParams } from "next/navigation";
import { ObserveRouteView } from "../../ObserveRoute";

export default function IncidentPage() {
  const { bundleId } = useParams<{ bundleId: string }>();
  return <ObserveRouteView route={{ kind: "incident", bundleId }} />;
}
