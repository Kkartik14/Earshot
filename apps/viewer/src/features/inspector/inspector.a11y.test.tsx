import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import toolTimeoutRetry from "./__fixtures__/faults/tool_timeout_retry.explanation.json";
import webrtcDegradation from "./__fixtures__/faults/webrtc_degradation.explanation.json";
import nativeInterruption from "./__fixtures__/faults/native_s2s_interruption.explanation.json";
import bargeIn from "./__fixtures__/faults/barge_in.explanation.json";
import { ApiError } from "../../api/client";
import { CallGraph } from "./CallGraph";
import {
  ClockCalibrationPanel,
  ContradictionsPanel,
  DiagnosesPanel,
  UnassignedPanel,
  contradictionsReason,
} from "./SessionFacts";
import { StageDrawer } from "./StageDrawer";
import { TurnDrawer } from "./TurnDrawer";
import { TurnTimeline } from "./TurnTimeline";
import {
  buildDiagnoses,
  buildTurnDetails,
  buildUnassigned,
  operationStatus,
  type ClockCalibrationView,
  type ContradictionView,
  type EdgeView,
  type ExplanationLike,
  type InterruptionChainView,
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
      {
        reactKey: `quality-${name}`,
        name: `earshot.${name}.ttfb`,
        value: 165,
        unit: "ms",
        confidence: "measured",
        aggregation: "instant",
        basis: "provider_measurement",
        evidenceIds: [`quality-${name}`],
      },
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
      limitation: null,
    },
  ],
  measurements: [],
  interruptionChains: [],
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

  it("renders an unavailable turn duration without inventing a zero", () => {
    const metric = {
      value: null,
      availability: "not_observed",
      basis: "clock_domain",
      confidence: "unavailable",
    };
    const timeline: Timeline = {
      scaleMs: 250,
      turns: [
        {
          turnId: "turn-cross-clock",
          index: 0,
          stages: [
            stage("llm", {
              startMs: null,
              endMs: null,
              leadMs: null,
              timing: "unavailable",
            }),
          ],
          firstToken: metric,
          generated: metric,
          response: metric,
          interrupted: false,
          hasCascade: false,
          totalMs: null,
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

    expect(screen.getByText("not observed")).toBeInTheDocument();
    expect(screen.queryByText("+0ms")).not.toBeInTheDocument();
    expect(screen.queryByText("+—")).not.toBeInTheDocument();
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
    // Both operation.failed and tool.retry cite this op, so it appears more than
    // once; clicking any chip reports the same turn + op id.
    const chip = screen.getAllByRole("button", { name: "op-tool-attempt-1" })[0];
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

  it("TurnDrawer renders repeated exact measurement facts separately from metrics", () => {
    render(
      <TurnDrawer
        detail={{
          ...turnDetail,
          measurements: [
            {
              reactKey: "quality-turn-1",
              name: "provider.queue_depth",
              value: 10,
              unit: "{item}",
              confidence: "measured",
              aggregation: "instant",
              basis: "provider_measurement",
              evidenceIds: ["quality-turn-1"],
              sourceField: "queue.depth.first",
            },
            {
              reactKey: "quality-turn-2",
              name: "provider.queue_depth",
              value: 20,
              unit: "{item}",
              confidence: "measured",
              aggregation: "instant",
              basis: "provider_measurement",
              evidenceIds: ["quality-turn-2"],
              sourceField: "queue.depth.second",
            },
          ],
        }}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    expect(screen.getByRole("heading", { name: /derived metrics/i })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /measurement facts/i }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("provider.queue_depth")).toHaveLength(2);
    expect(screen.getByText("10 item")).toBeInTheDocument();
    expect(screen.getByText("20 item")).toBeInTheDocument();
    expect(screen.getByText(/queue\.depth\.first.*quality-turn-1/)).toBeInTheDocument();
    expect(screen.getByText(/queue\.depth\.second.*quality-turn-2/)).toBeInTheDocument();
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
          reactKey: "quality-input-level",
          name: "earshot.audio.input_level",
          value: -21.4,
          unit: "dbfs",
          confidence: "measured",
          aggregation: "instant",
          basis: "provider_measurement",
          evidenceIds: ["quality-input-level"],
        },
        {
          reactKey: "quality-output",
          name: "earshot.stt.output",
          value: 42,
          unit: "{character}",
          confidence: "measured",
          aggregation: "instant",
          basis: "provider_measurement",
          evidenceIds: ["quality-output"],
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

describe("Interruption chain", () => {
  it("renders the ordered causal chain with its measured barge-in latency", () => {
    const detail = buildTurnDetails(bargeIn as unknown as ExplanationLike)[0];
    render(
      <TurnDrawer
        detail={detail}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    expect(
      screen.getByRole("heading", { name: /interruption chain/i }),
    ).toBeInTheDocument();
    // The chain is an ordered list, so assistive tech reads the causal sequence
    // and its position, not an undifferentiated pile of rows.
    const stages = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(stages.map((item) => item.firstElementChild?.textContent)).toEqual([
      "overlap observed",
      "intent",
      "classified",
      "cancellation requested",
      "generation stopped",
      "queued audio discarded",
      "transport stopped",
      "buffers purged",
      "render stopped",
      "resumed",
      "tool outcome",
    ]);
    expect(screen.getByText("barge-in effectiveness")).toBeInTheDocument();
    expect(screen.getByText("100ms")).toBeInTheDocument();
  });

  it("shows an unobserved stage as not observed with its reason, never as a zero", () => {
    const detail = buildTurnDetails(bargeIn as unknown as ExplanationLike)[0];
    render(
      <TurnDrawer
        detail={detail}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    const chain = within(screen.getByRole("list"));
    expect(chain.getAllByText("not observed · stage not observed")).toHaveLength(4);
    expect(chain.getByText("not observed · no tool in turn")).toBeInTheDocument();
    // An absent stage never borrows the origin's coordinate.
    expect(chain.queryAllByText("+0ms")).toHaveLength(0);
  });

  it("states an underivable barge-in latency instead of implying success", () => {
    const detail = buildTurnDetails(nativeInterruption as unknown as ExplanationLike)[0];
    render(
      <TurnDrawer
        detail={detail}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    const row = screen.getByText("barge-in effectiveness").parentElement as HTMLElement;
    expect(within(row).getByText("not observed")).toBeInTheDocument();
    expect(within(row).getByText("turn anchor not observed")).toBeInTheDocument();
    expect(within(row).queryByText("0ms")).not.toBeInTheDocument();
  });

  it("labels each episode when one turn produced more than one interruption", () => {
    const episode = (classification: string, key: string): InterruptionChainView => ({
      reactKey: key,
      turnId: "turn-0",
      classification,
      stages: [
        {
          stage: "overlap_observed",
          observed: true,
          atMs: 40,
          coordinate: null,
          evidenceId: "evt-overlap",
          coverageReason: null,
          outcome: null,
        },
      ],
      effectiveness: {
        key: "effectiveness",
        value: null,
        availability: "not_observed",
        basis: "interruption_barge_in",
        confidence: "unavailable",
        limitation: "target_signal_not_observed",
      },
    });
    render(
      <TurnDrawer
        detail={{
          ...turnDetail,
          interruptionChains: [episode("accepted", "a"), episode("false", "b")],
        }}
        coverage={[]}
        onClose={() => {}}
        onPickStage={() => {}}
      />,
    );

    expect(
      screen.getByRole("article", { name: /interruption 1 of 2 · accepted/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("article", { name: /interruption 2 of 2 · false/i }),
    ).toBeInTheDocument();
  });
});

describe("Contradictions panel", () => {
  const contradiction: ContradictionView = {
    reactKey: "render_claim_conflict:turn-1:0",
    kind: "render_claim_conflict",
    summary: "render_observed_while_coverage_not_observed",
    boundary: "render",
    turnId: "turn-1",
    evidence: [
      { id: "op-tool-attempt-1", turnIndex: 0 },
      { id: "quality-sample-9", turnIndex: null },
    ],
  };

  it("is a labelled region and selects the evidence operation on click", () => {
    const onSelect = vi.fn();
    render(
      <ContradictionsPanel
        status="ready"
        reason={null}
        contradictions={[contradiction]}
        onSelectEvidence={onSelect}
      />,
    );

    const region = within(screen.getByRole("region", { name: /contradictions/i }));
    expect(region.getByText("render claim conflict")).toBeInTheDocument();
    expect(region.getByText("render")).toBeInTheDocument();
    fireEvent.click(region.getByRole("button", { name: "op-tool-attempt-1" }));
    expect(onSelect).toHaveBeenCalledWith(0, "op-tool-attempt-1");
    // Evidence that names no operation in a turn is not made falsely selectable.
    expect(region.queryByRole("button", { name: "quality-sample-9" })).toBeNull();
    expect(region.getByText("quality-sample-9")).toBeInTheDocument();
  });

  it("says detection did not run rather than reporting zero conflicts", () => {
    render(
      <ContradictionsPanel
        status="unavailable"
        reason="EARSHOT_ANALYSIS_NOT_AVAILABLE"
        contradictions={[]}
        onSelectEvidence={() => {}}
      />,
    );

    const region = within(screen.getByRole("region", { name: /contradictions/i }));
    expect(region.getByText(/did not run/i)).toBeInTheDocument();
    expect(region.getByText(/EARSHOT_ANALYSIS_NOT_AVAILABLE/)).toBeInTheDocument();
    expect(region.getByText(/unknown, not resolved/i)).toBeInTheDocument();
    // No count badge: "0" would read as a clean bill of health.
    expect(region.queryByText("0")).toBeNull();
  });

  it("names a backend refusal by its code and an unreachable backend as such", () => {
    expect(
      contradictionsReason(new ApiError(404, "EARSHOT_ANALYSIS_NOT_AVAILABLE")),
    ).toBe("EARSHOT_ANALYSIS_NOT_AVAILABLE");
    expect(contradictionsReason(new TypeError("Failed to fetch"))).toBe(
      "the backend did not answer",
    );
    expect(contradictionsReason(null)).toBe("the backend did not answer");
  });

  it("reports an examined incident with no conflicts as examined", () => {
    render(
      <ContradictionsPanel
        status="ready"
        reason={null}
        contradictions={[]}
        onSelectEvidence={() => {}}
      />,
    );

    const region = within(screen.getByRole("region", { name: /contradictions/i }));
    expect(region.getByText("0")).toBeInTheDocument();
    expect(region.getByText(/detection ran/i)).toBeInTheDocument();
  });
});

describe("Clock comparability panel", () => {
  const calibration: ClockCalibrationView = {
    domains: [
      {
        id: "server-clock",
        kind: "process_monotonic",
        observer: "server",
        uncertaintyMs: null,
      },
      { id: "device-clock", kind: "device_wall", observer: "browser", uncertaintyMs: 2 },
    ],
    relations: [
      {
        relationId: "rel-1",
        fromDomain: "device-clock",
        toDomain: "server-clock",
        method: "ntp_offset",
        uncertaintyMs: 3,
        driftPpm: null,
      },
    ],
    crossClock: [
      {
        reactKey: "turn-1:response",
        turnIndex: 0,
        turnId: "turn-1",
        metric: "response",
        state: "estimated",
        availability: "available",
        note: "estimated across clock domains through a declared calibration",
      },
      {
        reactKey: "turn-1:render_start",
        turnIndex: 1,
        turnId: "turn-2",
        metric: "render_start",
        state: "unavailable",
        availability: "not_observed",
        note: "two clock domains with no declared calibration between them",
      },
    ],
  };

  it("shows the calibration uncertainty an estimated latency carries", () => {
    render(<ClockCalibrationPanel calibration={calibration} />);

    const region = within(screen.getByRole("region", { name: /clock comparability/i }));
    expect(region.getByText("device-clock → server-clock")).toBeInTheDocument();
    expect(region.getByText("±3ms")).toBeInTheDocument();
    expect(region.getByText("T00 · response")).toBeInTheDocument();
    expect(region.getByText("estimated")).toBeInTheDocument();
  });

  it("names the missing calibration behind an unavailable latency", () => {
    render(<ClockCalibrationPanel calibration={calibration} />);

    const region = within(screen.getByRole("region", { name: /clock comparability/i }));
    expect(region.getByText("T01 · render_start")).toBeInTheDocument();
    expect(region.getByText("not observed")).toBeInTheDocument();
    expect(
      region.getByText("two clock domains with no declared calibration between them"),
    ).toBeInTheDocument();
  });

  it("reports an undeclared relation uncertainty as undeclared, not as zero", () => {
    render(
      <ClockCalibrationPanel
        calibration={{
          ...calibration,
          relations: [{ ...calibration.relations[0], uncertaintyMs: null }],
        }}
      />,
    );

    expect(screen.getByText("uncertainty not declared")).toBeInTheDocument();
    expect(screen.queryByText("±0ms")).toBeNull();
  });

  it("states plainly when no calibration is declared at all", () => {
    render(<ClockCalibrationPanel calibration={{ ...calibration, relations: [] }} />);

    expect(screen.getByText(/no clock calibration is declared/i)).toBeInTheDocument();
  });

  it("renders nothing for a single-clock session with no cross-clock latency", () => {
    const { container } = render(
      <ClockCalibrationPanel
        calibration={{ domains: [calibration.domains[0]], relations: [], crossClock: [] }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
