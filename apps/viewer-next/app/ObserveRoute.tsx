"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ObserveFeature,
  type ObserveRoute,
  type ViewerLinkProps,
} from "@earshot/viewer-ui";

function NextLink({ href, ...props }: ViewerLinkProps) {
  return <Link href={href} {...props} />;
}

export function ObserveRouteView({ route }: { route: ObserveRoute }) {
  const currentPath = usePathname() ?? "/";
  return (
    <ObserveFeature route={route} currentPath={currentPath} LinkComponent={NextLink} />
  );
}
