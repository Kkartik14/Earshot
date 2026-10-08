import type { ComponentType, ReactNode } from "react";

export interface ViewerLinkProps {
  href: string;
  className?: string;
  children: ReactNode;
}

/** A host-provided link keeps Observe independent of React Router and Next.js. */
export type ViewerLinkComponent = ComponentType<ViewerLinkProps>;

export const AnchorLink: ViewerLinkComponent = ({ href, className, children }) => (
  <a href={href} className={className}>
    {children}
  </a>
);

export type ObserveRoute =
  | { kind: "fleet" }
  | { kind: "incident"; bundleId: string }
  | { kind: "session"; sessionId: string }
  | { kind: "live"; sessionId: string }
  | {
      kind: "call-status";
      callId: string;
      capture:
        | { state: "waiting"; detail?: string }
        | { state: "delayed"; detail?: string }
        | { state: "missing"; detail?: string }
        | { state: "rejected"; reasonCode: string; detail?: string };
    };
