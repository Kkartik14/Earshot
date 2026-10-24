"use client";

import { useParams } from "next/navigation";
import { ObserveRouteView } from "../../ObserveRoute";

export default function CallPage() {
  const { callId } = useParams<{ callId: string }>();
  return <ObserveRouteView route={{ kind: "call", callId }} />;
}
