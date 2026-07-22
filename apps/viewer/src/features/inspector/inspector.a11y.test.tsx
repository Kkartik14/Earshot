import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CallGraph } from "./CallGraph";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import type { OperationRole, StageDetail, StageName, TurnDetail } from "./timeline";

function stage(name: StageName, over: Partial<StageDetail> = {}): StageDetail {
  return {
    operationId: `op-${name}`,
    name,
    role: name,
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

/** A non-cascade operation (tool / agent / transport / …) for generic tests. */
function op(
  operationId: string,
  name: string,
  role: OperationRole,
  over: Partial<StageDetail> = {},
): StageDetail {
  return {
    operationId,
    name,
    role,
    status: "ok",
    startMs: 0,
    endMs: 50,
    leadMs: null,
    timing: "interval",
    measurements: [],
    ...over,
  };
}

const turnDetail: TurnDetail = {
  turnId: "turn-0",
  index: 0,
  interrupted: false,
  hasCascade: true,
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
  it("activates an operation node with click, Enter, and Space", () => {
    const onPick = vi.fn();
    render(<CallGraph detail={turnDetail} onPick={onPick} />);
    const node = screen.getByRole("button", { name: /stt operation/i });

    fireEvent.click(node);
    fireEvent.keyDown(node, { key: "Enter" });
    fireEvent.keyDown(node, { key: " " });

    expect(onPick).toHaveBeenCalledTimes(3);
    expect(onPick).toHaveBeenCalledWith("op-stt");
  });

  it("exposes one interactive node per operation and labels the graph", () => {
    const { container } = render(<CallGraph detail={turnDetail} onPick={() => {}} />);
    // The generic graph renders exactly one operable node per operation — no
    // invented playout node, no fixed barge row.
    expect(screen.getAllByRole("button")).toHaveLength(turnDetail.stages.length);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("role", "group");
    expect(svg?.getAttribute("aria-label")).toMatch(/call graph/i);
    // The description is generated from the real operations, in arrival order.
    expect(svg).toHaveAccessibleDescription(
      /operations in arrival order.*stt.*llm.*tts/i,
    );
    expect(svg).toHaveAccessibleDescription(/does not imply causation/i);
  });

  it("renders a single native speech-to-speech agent operation", () => {
    const s2s: TurnDetail = {
      ...turnDetail,
      hasCascade: false,
      stages: [op("op-native-agent", "agent", "agent")],
    };
    render(<CallGraph detail={s2s} onPick={() => {}} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /agent operation/i })).toBeInTheDocument();
  });

  it("renders a tool call and its same-named retry as distinct addressable nodes", () => {
    const onPick = vi.fn();
    const retried: TurnDetail = {
      ...turnDetail,
      hasCascade: false,
      stages: [
        op("op-tool-attempt-1", "tool", "tool", { status: "timeout" }),
        op("op-tool-attempt-2", "tool", "tool"),
        op("op-downstream-agent", "agent", "agent"),
      ],
    };
    render(<CallGraph detail={retried} onPick={onPick} />);
    // Both tool attempts are present and separately reachable despite the shared name.
    const toolNodes = screen.getAllByRole("button", { name: /tool operation/i });
    expect(toolNodes).toHaveLength(2);
    fireEvent.click(toolNodes[0]);
    fireEvent.click(toolNodes[1]);
    expect(onPick).toHaveBeenNthCalledWith(1, "op-tool-attempt-1");
    expect(onPick).toHaveBeenNthCalledWith(2, "op-tool-attempt-2");
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

  it("StageDrawer formats a non-duration measurement by its real unit", () => {
    const noise = stage("stt", {
      measurements: [
        {
          name: "earshot.audio.input_level",
          value: -21.4,
          unit: "dbfs",
          confidence: "measured",
        },
        {
          name: "earshot.stt.output",
          value: 42,
          unit: "{character}",
          confidence: "measured",
        },
      ],
    });
    render(<StageDrawer index={0} stage={noise} onClose={() => {}} />);
    // Formatted with the declared unit — never mislabelled as milliseconds.
    expect(screen.getByText("-21.4 dbfs")).toBeInTheDocument();
    expect(screen.getByText("42 character")).toBeInTheDocument();
    expect(screen.queryByText(/-21ms/)).not.toBeInTheDocument();
  });
});
