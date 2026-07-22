import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CallGraph } from "./CallGraph";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import type { StageDetail, StageName, TurnDetail } from "./timeline";

function stage(name: StageName, over: Partial<StageDetail> = {}): StageDetail {
  return {
    name,
    provider: "groq",
    model: name === "stt" ? "whisper-large-v3-turbo" : "llama-3.1-8b-instant",
    status: "ok",
    startMs: 0,
    endMs: null,
    leadMs: name === "llm" ? 240 : 165,
    timing: "point",
    evidence: {
      source: "app",
      observer: "server",
      method: "pipeline_capture",
      confidence: "inferred",
      sourceField: "sha256:abc",
    },
    measurements: [
      { name: `earshot.${name}.ttfb`, value: 165, unit: "ms", confidence: "measured" },
    ],
    ...over,
  };
}

const turnDetail: TurnDetail = {
  turnId: "turn-0",
  index: 0,
  interrupted: false,
  firstTokenMs: 240,
  stages: [stage("stt"), stage("llm"), stage("tts")],
  metrics: [
    {
      key: "first_token",
      value: 240,
      availability: "available",
      basis: "provider_stage_direct",
      confidence: "measured",
    },
  ],
  events: [
    {
      name: "earshot.speech.ended",
      atMs: 0,
      participant: "user",
      confidence: "measured",
    },
  ],
};

describe("CallGraph accessibility", () => {
  it("activates a stage node with click, Enter, and Space", () => {
    const onPick = vi.fn();
    render(<CallGraph detail={turnDetail} onPick={onPick} />);
    const node = screen.getByRole("button", { name: /stt stage/i });

    fireEvent.click(node);
    fireEvent.keyDown(node, { key: "Enter" });
    fireEvent.keyDown(node, { key: " " });

    expect(onPick).toHaveBeenCalledTimes(3);
    expect(onPick).toHaveBeenCalledWith("stt");
  });

  it("exposes only the interactive nodes and labels the graph", () => {
    const { container } = render(<CallGraph detail={turnDetail} onPick={() => {}} />);
    // stt, llm, tts are operable; the terminal playout node and edge labels are hidden.
    expect(screen.getAllByRole("button")).toHaveLength(3);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("role", "group");
    expect(svg?.getAttribute("aria-label")).toMatch(/call graph/i);
    expect(svg).toHaveAccessibleDescription(
      /stt transcribes to llm.*tts emits playout.*client render.*not observed/i,
    );
  });
});

describe("Detail drawers", () => {
  it("TurnDrawer is a labelled dialog, focuses close on open, and has headings", () => {
    const onClose = vi.fn();
    render(
      <TurnDrawer
        detail={turnDetail}
        coverage={[]}
        onClose={onClose}
        onPickStage={() => {}}
      />,
    );

    expect(screen.getByRole("dialog", { name: /turn 0 detail/i })).toBeInTheDocument();
    const close = screen.getByRole("button", { name: /close detail/i });
    expect(close).toHaveFocus();
    expect(screen.getByRole("heading", { name: /call graph/i })).toBeInTheDocument();

    fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("StageDrawer is a labelled dialog that focuses its close control", () => {
    const onClose = vi.fn();
    render(<StageDrawer index={0} stage={stage("llm")} onClose={onClose} />);

    expect(
      screen.getByRole("dialog", { name: /turn 0 llm detail/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /close detail/i })).toHaveFocus();
    expect(
      screen.getByRole("heading", { name: /provider measurement/i }),
    ).toBeInTheDocument();
  });
});
