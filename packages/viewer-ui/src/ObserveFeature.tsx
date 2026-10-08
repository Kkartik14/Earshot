"use client";

import { FleetDashboard } from "./features/fleet/FleetDashboard";
import { SessionInspector } from "./features/inspector/SessionInspector";
import type { IncidentReference } from "./features/inspector/IncidentReferences";
import { LiveSessionView } from "./features/live/LiveSessionView";
import { CallCaptureStatus } from "./features/sessions/CallCaptureStatus";
import { SessionResolver } from "./features/sessions/SessionResolver";
import { SessionRail } from "./features/sessions/SessionRail";
import { useViewerQueryScope } from "./api/queryScope";
import { AnchorLink, type ObserveRoute, type ViewerLinkComponent } from "./navigation";
import styles from "./ObserveFeature.module.css";

export function ObserveFeature({
  route,
  currentPath,
  LinkComponent = AnchorLink,
  hrefForReference,
  embedded = false,
}: {
  route: ObserveRoute;
  currentPath?: string;
  LinkComponent?: ViewerLinkComponent;
  hrefForReference?: (reference: IncidentReference) => string | undefined;
  embedded?: boolean;
}) {
  const scope = useViewerQueryScope();
  const Content = embedded ? "div" : "main";
  return (
    <div
      key={`${scope.projectId}\u0000${scope.authContextId}`}
      className={styles.shell}
      data-earshot-viewer
      data-embedded={embedded ? "true" : undefined}
    >
      {embedded ? null : (
        <SessionRail currentPath={currentPath} LinkComponent={LinkComponent} />
      )}
      <Content className={styles.main} data-embedded={embedded ? "true" : undefined}>
        {route.kind === "fleet" ? <FleetDashboard /> : null}
        {route.kind === "incident" ? (
          <SessionInspector
            bundleId={route.bundleId}
            hrefForReference={hrefForReference}
          />
        ) : null}
        {route.kind === "session" ? (
          <SessionResolver
            sessionId={route.sessionId}
            LinkComponent={LinkComponent}
            hrefForReference={hrefForReference}
          />
        ) : null}
        {route.kind === "live" ? (
          <LiveSessionView sessionId={route.sessionId} LinkComponent={LinkComponent} />
        ) : null}
        {route.kind === "call-status" ? (
          <CallCaptureStatus callId={route.callId} capture={route.capture} />
        ) : null}
      </Content>
    </div>
  );
}
