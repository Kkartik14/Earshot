import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CallCaptureStatus } from "./CallCaptureStatus";

describe("CallCaptureStatus", () => {
  it("reports a host-rejected upload without presenting invented incident evidence", () => {
    render(
      <CallCaptureStatus
        callId="call_123"
        capture={{ state: "rejected", reasonCode: "EARSHOT_INVALID_INCIDENT" }}
      />,
    );

    expect(screen.getByRole("heading", { name: "call_123" })).toBeInTheDocument();
    expect(screen.getByText("Call evidence was rejected")).toBeInTheDocument();
    expect(screen.getByText(/host-reported delivery state/i)).toBeInTheDocument();
    expect(screen.getByText(/EARSHOT_INVALID_INCIDENT/)).toBeInTheDocument();
    expect(screen.queryByText(/transcript/i)).not.toBeInTheDocument();
  });
});
