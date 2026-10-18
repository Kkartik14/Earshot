import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IncidentReferences } from "./IncidentReferences";

const { useExternalReferences } = vi.hoisted(() => ({
  useExternalReferences: vi.fn(),
}));

vi.mock("../../api/hooks", () => ({ useExternalReferences }));

const callReference = {
  namespace: "platform",
  record_type: "call",
  external_id: "call_123",
  linked_at_unix_nano: "1",
};

describe("IncidentReferences", () => {
  beforeEach(() => {
    useExternalReferences.mockReset();
  });

  it("renders external IDs as text unless the host provides a safe local route", () => {
    useExternalReferences.mockReturnValue({
      isPending: false,
      isError: false,
      isSuccess: true,
      data: { bundle_id: "bundle-1", items: [callReference] },
    });

    const { rerender } = render(<IncidentReferences bundleId="bundle-1" />);
    expect(screen.getByText("Platform call")).toBeInTheDocument();
    expect(screen.getByText("call_123")).not.toHaveAttribute("href");

    rerender(
      <IncidentReferences
        bundleId="bundle-1"
        hrefForReference={() => "/projects/p/calls/call_123"}
      />,
    );
    expect(screen.getByRole("link", { name: "call_123" })).toHaveAttribute(
      "href",
      "/projects/p/calls/call_123",
    );
  });

  it("does not turn host-provided unsafe URLs into clickable links", () => {
    useExternalReferences.mockReturnValue({
      isPending: false,
      isError: false,
      isSuccess: true,
      data: { bundle_id: "bundle-1", items: [callReference] },
    });

    render(
      <IncidentReferences
        bundleId="bundle-1"
        hrefForReference={() => "javascript:alert(1)"}
      />,
    );
    expect(screen.getByText("call_123")).not.toHaveAttribute("href");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
