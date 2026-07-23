import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import toolTimeoutRetry from "./__fixtures__/faults/tool_timeout_retry.explanation.json";
import webrtcDegradation from "./__fixtures__/faults/webrtc_degradation.explanation.json";
import nativeInterruption from "./__fixtures__/faults/native_s2s_interruption.explanation.json";
import { CallGraph } from "./CallGraph";
import { DiagnosesPanel, UnassignedPanel } from "./SessionFacts";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline } from "./TurnTimeline";
import {
  buildDiagnoses,
  buildTurnDetails,
  buildUnassigned,
  operationStatus,
  type EdgeView,
  type ExplanationLike,
  type OperationRole,
  type StageDetail,
  type StageName,
  type Timeline,
  type TurnDetail,
} from "./timeline";

function stage(name: StageName, over: Partial<StageDetail> = {}): StageDetail {
  const status = over.status ?? "ok";
  return {
    operationId: `op-${name}`,
    name,
    role: name,
    provider: "groq",
    model: name === "stt" ? "whisper-large-v3-turbo" : "llama-3.1-8b-instant",
    status,
    statusView: operationStatus({ status }),
    startMs: 0,
    endMs: null,
    leadMs: name === "llm" ? 240 : 165,
    timing: "point",
    startUncertaintyMs: null,
    endUncertaintyMs: null,
    links: [],
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
  const status = over.status ?? "ok";
  return {
    operationId,
    name,
    role,
    status,
    statusView: operationStatus({ status }),
    startMs: 0,
    endMs: 50,
    leadMs: null,
    timing: "interval",
    startUncertaintyMs: null,
    endUncertaintyMs: null,
    links: [],
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
  edges: [],
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
      attachedOperationId: null,
    },
  ],
};

describe("CallGraph accessibility", () => {
  it("preserves the exact source-authored model identity", () => {
    const exactModel = "llama-4-maverick-17b-128e-instruct";
    render(
      <CallGraph
        detail={{
          ...turnDetail,
          stages: [stage("llm", { model: exactModel })],
        }}
        onPick={() => {}}
      />,
    );

    expect(
      screen.getByRole("button", { name: new RegExp(exactModel) }),
    ).toBeInTheDocument();
    expect(screen.queryByText("llama-3.1")).not.toBeInTheDocument();
  });

  it("does not attribute a turn-level first-token value to the LLM node", () => {
    render(<CallGraph detail={{ ...turnDetail, firstTokenMs: 720 }} onPick={() => {}} />);

    const llmNode = screen.getByRole("button", { name: /llm operation/i });
    const visualClasses = [llmNode, ...llmNode.querySelectorAll("[class]")]
      .map((element) => element.getAttribute("class") ?? "")
      .join(" ");
    expect(visualClasses).not.toMatch(/slow/i);
  });

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
    // With no links, the description states that plainly — it never fabricates a
    // causal or arrival-order connector.
    expect(svg).toHaveAccessibleDescription(/no causal links were recorded/i);
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

  it("draws the real retry edge between the two tool attempts (from links, not arrival order)", () => {
    // Use the actual backend projection: attempt-2 `retries` attempt-1,
    // downstream-agent `consumes` attempt-2.
    const detail = buildTurnDetails(toolTimeoutRetry as unknown as ExplanationLike)[0];
    const retryEdge = detail.edges.find((e: EdgeView) => e.relationship === "retries");
    expect(retryEdge).toEqual({
      fromOperationId: "op-tool-attempt-2",
      toOperationId: "op-tool-attempt-1",
      relationship: "retries",
    });

    const { container } = render(<CallGraph detail={detail} onPick={() => {}} />);
    const svg = container.querySelector("svg");
    // The causal edges are described, and their labels are drawn.
    expect(svg).toHaveAccessibleDescription(/retries/i);
    expect(svg).toHaveAccessibleDescription(/consumes/i);
    expect(screen.getByText("retries")).toBeInTheDocument();
    expect(screen.getByText("consumes")).toBeInTheDocument();
  });

  it("describes an observed parent edge without calling it a causal link", () => {
    const parented: TurnDetail = {
      ...turnDetail,
      hasCascade: false,
      stages: [op("op-parent", "agent", "agent"), op("op-child", "tool", "tool")],
      edges: [
        {
          fromOperationId: "op-parent",
          toOperationId: "op-child",
          relationship: "parent",
        },
      ],
    };

    const { container } = render(<CallGraph detail={parented} onPick={() => {}} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAccessibleDescription(/agent parents tool/i);
    expect(svg).not.toHaveAccessibleDescription(/^causal links/i);
  });

  it("badges the timed-out tool attempt on its node", () => {
    const detail = buildTurnDetails(toolTimeoutRetry as unknown as ExplanationLike)[0];
    render(<CallGraph detail={detail} onPick={() => {}} />);
    // The timeout status surfaces as a badge on the node, and in its label.
    expect(screen.getByText("timeout")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /tool operation.*status timeout/i }),
    ).toBeInTheDocument();
  });

  it("shows a start uncertainty as a ± annotation without dropping it", () => {
    const uncertain: TurnDetail = {
      ...turnDetail,
      hasCascade: false,
      stages: [op("op-u", "tool", "tool", { leadMs: 120, startUncertaintyMs: 5 })],
    };
    render(<CallGraph detail={uncertain} onPick={() => {}} />);
    expect(screen.getByRole("button", { name: /120ms ±5ms/i })).toBeInTheDocument();
  });
});

describe("Turn timeline truthfulness", () => {
  it("renders measured high latency without an invented slow verdict or LLM glow", () => {
    const metric = {
      value: 720,
      availability: "available",
      basis: "provider_stage_direct",
      confidence: "measured",
    };
    const timeline: Timeline = {
      scaleMs: 1_000,
      turns: [
        {
          turnId: "turn-high-latency",
          index: 0,
          stages: [
            stage("llm", {
              startMs: 0,
              endMs: 900,
              leadMs: 720,
              timing: "interval",
            }),
          ],
          firstToken: metric,
          generated: { ...metric, value: null, availability: "not_observed" },
          response: { ...metric, value: null, availability: "not_observed" },
          interrupted: false,
          hasCascade: false,
          totalMs: 900,
        },
      ],
    };

    render(
      <TurnTimeline
        timeline={timeline}
        openTurns={new Set()}
        selection={null}
        onToggleTurn={() => {}}
        onSelectOperation={() => {}}
      />,
    );

    expect(screen.queryByText("slow")).not.toBeInTheDocument();
    expect(
      screen.getByTitle("llm observed interval · groq").getAttribute("class"),
    ).not.toMatch(/glow/i);
  });
});

describe("Interruption attachment", () => {
  it("keeps an interruption turn-level when it references no operation (no false row)", () => {
    // The native s2s projection carries an interruption event with no stream/op
    // correlation — it must NOT attach to the single agent operation.
    const detail = buildTurnDetails(nativeInterruption as unknown as ExplanationLike)[0];
    expect(detail.stages.every((s) => s.interruptedByEvent == null)).toBe(true);
    const turnEvent = detail.events.find((e) => e.name.includes("interruption"));
    expect(turnEvent?.attachedOperationId).toBeNull();
  });

  it("attaches an interruption to the operation it explicitly references", () => {
    const explanation = {
      bundle_id: "b",
      session_id: "s",
      session_status: "completed",
      finality: "final",
      completeness: "complete",
      analyzer_version: "t",
      limitations: [],
      coverage: [],
      omissions: [],
      turns: [
        {
          turn_id: "turn-1",
          metrics: {},
          operations: [
            {
              operation_id: "op-tts",
              operation_name: "tts",
              status: "cancelled",
              shape: "interval",
              time_basis: "monotonic",
              clock_domain_id: "c",
              start_nano: "1000",
              duration_nano: "500000000",
              stream_id: "stream-output",
              measurements: [],
            },
          ],
          events: [
            {
              event_id: "evt-interruption",
              event_name: "earshot.interruption.accepted",
              operation_id: "op-tts",
              time_basis: "monotonic",
              clock_domain_id: "c",
              at_nano: "1200",
              stream_id: "stream-output",
              evidence_ids: ["evt-interruption"],
            },
          ],
        },
      ],
    } as unknown as ExplanationLike;
    const detail = buildTurnDetails(explanation)[0];
    expect(detail.stages[0].interruptedByEvent).toBe("earshot.interruption.accepted");
    expect(detail.events[0].attachedOperationId).toBe("op-tts");
    render(<CallGraph detail={detail} onPick={() => {}} />);
    expect(
      screen.getByRole("button", { name: /tts operation.*interrupted/i }),
    ).toBeInTheDocument();
  });
});

describe("Diagnoses panel", () => {
  it("shows the operation.failed diagnosis and links to its evidence operation", () => {
    const explanation = toolTimeoutRetry as unknown as ExplanationLike;
    const diagnoses = buildDiagnoses(explanation);
    const onSelect = vi.fn();
    render(<DiagnosesPanel diagnoses={diagnoses} onSelectEvidence={onSelect} />);

    expect(screen.getByRole("heading", { name: /diagnoses/i })).toBeInTheDocument();
    expect(screen.getByText("operation.failed")).toBeInTheDocument();
    // The evidence operation is a selectable chip that reports its turn + op id.
    const chip = screen.getByRole("button", { name: "op-tool-attempt-1" });
    fireEvent.click(chip);
    expect(onSelect).toHaveBeenCalledWith(0, "op-tool-attempt-1");
  });
});

describe("Session-level facts", () => {
  it("renders webrtc unassigned measurements with their real units", () => {
    const facts = buildUnassigned(webrtcDegradation as unknown as ExplanationLike);
    expect(facts.measurements).toHaveLength(3);
    const { container } = render(<UnassignedPanel facts={facts} />);
    const region = within(container);
    // jitter and rtt are milliseconds; packet loss is a bare ratio (unit "1").
    expect(region.getByText("jitter")).toBeInTheDocument();
    expect(region.getByText("42ms")).toBeInTheDocument();
    expect(region.getByText("round_trip_time")).toBeInTheDocument();
    expect(region.getByText("180ms")).toBeInTheDocument();
    expect(region.getByText("packet_loss_ratio")).toBeInTheDocument();
    expect(region.getByText("0.18")).toBeInTheDocument();
  });

  it("renders source-authored events that are not scoped to a turn", () => {
    const explanation = {
      ...webrtcDegradation,
      unassigned_events: [
        {
          event_id: "evt-session-reconnecting",
          event_name: "earshot.transport.reconnecting",
          time_basis: "monotonic",
          clock_domain_id: "server-clock",
          at_nano: "800000000",
          evidence: { confidence: "measured" },
          evidence_ids: ["evt-session-reconnecting"],
        },
      ],
    } as unknown as ExplanationLike;

    const facts = buildUnassigned(explanation);
    expect(facts.events).toEqual([
      {
        eventId: "evt-session-reconnecting",
        name: "earshot.transport.reconnecting",
        coordinate: "server-clock · monotonic · 800000000ns",
        confidence: "measured",
      },
    ]);

    render(<UnassignedPanel facts={facts} />);
    const eventName = screen.getByText("earshot.transport.reconnecting");
    const eventFact = eventName.closest("article");
    expect(eventFact).not.toBeNull();
    const eventRegion = within(eventFact as HTMLElement);
    expect(
      eventRegion.getByText("server-clock · monotonic · 800000000ns"),
    ).toBeInTheDocument();
    expect(eventRegion.getByText("measured")).toBeInTheDocument();
  });
});

describe("Detail drawers", () => {
  it("shows high first-token latency without inventing a budget verdict", () => {
    render(
      <TurnDrawer
        detail={{ ...turnDetail, firstTokenMs: 720 }}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    expect(screen.getByText("720")).toBeInTheDocument();
    expect(screen.getByText("first token")).toBeInTheDocument();
    expect(screen.queryByText("slow")).not.toBeInTheDocument();
    expect(screen.queryByText(/budget/i)).not.toBeInTheDocument();
  });

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

  it("labels STT TTFB as time to first response, not finalization", () => {
    render(<StageDrawer index={0} stage={stage("stt")} onClose={() => {}} />);

    expect(screen.getByText("time to first response")).toBeInTheDocument();
    expect(screen.queryByText("finalization")).not.toBeInTheDocument();
  });

  it("StageDrawer badges an errored operation with its code and category", () => {
    const errored = op("op-x", "tool", "tool", {
      status: "failed",
      statusView: operationStatus({
        status: "failed",
        error: {
          code: "tool_timeout",
          category: "timeout",
          capture_class: "metadata",
        },
      }),
    });
    render(<StageDrawer index={0} stage={errored} onClose={() => {}} />);
    // The code · category shows both as the header badge and in the error row.
    expect(screen.getAllByText("tool_timeout · timeout")).toHaveLength(2);
  });

  it("StageDrawer lists a resolved causal link to its target operation", () => {
    const linked = op("op-agent", "agent", "agent", {
      links: [
        {
          relationship: "consumes",
          targetOperationId: "op-tool-attempt-2",
          targetScope: "internal",
          resolved: true,
        },
      ],
    });
    render(<StageDrawer index={0} stage={linked} onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: /causal links/i })).toBeInTheDocument();
    expect(screen.getByText("op-tool-attempt-2")).toBeInTheDocument();
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
