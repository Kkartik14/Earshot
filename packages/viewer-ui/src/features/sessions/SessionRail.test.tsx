import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useIncidents, useLiveSessions } from "../../api/hooks";
import { SessionRail } from "./SessionRail";

const hookMocks = vi.hoisted(() => ({
  useIncidents: vi.fn(),
  useLiveSessions: vi.fn(),
}));

vi.mock("../../api/hooks", () => hookMocks);
vi.mock("../../components/Waveform", () => ({ Waveform: () => null }));

beforeEach(() => {
  vi.mocked(useIncidents).mockReturnValue({
    data: { items: [] },
    isPending: false,
    isError: false,
    isSuccess: true,
  } as never);
});

describe("SessionRail", () => {
  it("distinguishes an unavailable live-session list from an empty one", () => {
    vi.mocked(useLiveSessions).mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
    } as never);

    render(<SessionRail />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      /active sessions are unavailable/i,
    );
  });
});
