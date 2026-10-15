import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { ExportPanel } from "./ExportPanel";

const state = vi.hoisted(() => ({ export: null as unknown }));

vi.mock("../../api/hooks", () => ({
  useExport: () => state.export,
  // The real ExportFormat type is erased at runtime; the panel only needs the
  // hook and the format list from ./exporters, which stays real.
}));

const project = () =>
  fireEvent.click(screen.getByRole("button", { name: /project through exporter/i }));

beforeEach(() => {
  state.export = { data: undefined, isPending: false, isError: false };
});

describe("ExportPanel", () => {
  it("exposes an accessibly-named, keyboard-reachable format picker and trigger", () => {
    render(<ExportPanel bundleId="fix" />);
    const format = screen.getByLabelText("Exporter format");
    expect(format).toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: /project through exporter/i });
    trigger.focus();
    expect(trigger).toHaveFocus();
    // Nothing is projected until the reader asks for it.
    expect(screen.getByText(/choose an exporter and project/i)).toBeInTheDocument();
  });

  it("presents the projection and offers it as a download", () => {
    state.export = {
      data: {
        bundle_id: "fix",
        digest: "d".repeat(64),
        format: "otlp",
        destination: "otel_collector",
        document: { resourceSpans: [] },
      },
      isPending: false,
      isError: false,
    };
    render(<ExportPanel bundleId="fix" />);
    project();

    expect(screen.getByText("otel_collector")).toBeInTheDocument();
    expect(screen.getByText(/resourceSpans/)).toBeInTheDocument();
    const download = screen.getByRole("link", { name: /download projection/i });
    expect(download).toHaveAttribute("download", "fix.otlp.json");
    expect(download.getAttribute("href")).toMatch(/^data:application\/json/);
  });

  it("renders a policy denial as an explicit refusal, not an empty document", () => {
    state.export = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(403, "EARSHOT_EXPORT_DENIED"),
    };
    render(<ExportPanel bundleId="fix" />);
    project();

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/denied by policy/i);
    expect(status).toHaveTextContent(/EARSHOT_EXPORT_DENIED/);
    expect(status).toHaveTextContent(/no document was produced/i);
    expect(screen.queryByRole("link", { name: /download/i })).not.toBeInTheDocument();
  });

  it("renders an unregistered exporter name as an explicit refusal", () => {
    state.export = {
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(400, "EARSHOT_UNKNOWN_EXPORT_FORMAT"),
    };
    render(<ExportPanel bundleId="fix" />);
    project();

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/unknown exporter/i);
    expect(status).toHaveTextContent(/EARSHOT_UNKNOWN_EXPORT_FORMAT/);
  });
});
