import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import toolTimeoutRetry from "./__fixtures__/faults/tool_timeout_retry.explanation.json";
import telephonyHandoff from "./__fixtures__/faults/telephony_handoff.explanation.json";
import websocketReconnect from "./__fixtures__/faults/websocket_reconnect.explanation.json";
import webrtcDegradation from "./__fixtures__/faults/webrtc_degradation.explanation.json";
import nativeInterruption from "./__fixtures__/faults/native_s2s_interruption.explanation.json";
import {
  buildDiagnoses,
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  buildUnassigned,
  getCoverage,
  type AnalysisLike,
  type ExplanationLike,
  type IncidentLike,
} from "./timeline";

const asExplanation = (fixture: unknown): ExplanationLike =>
  fixture as unknown as ExplanationLike;

const incident = incidentFixture as unknown as IncidentLike;
const analysis = analysisFixture as unknown as AnalysisLike;
const explanation = {
  bundle_id: "fixture-bundle",
  session_id: "fixture-session",
  session_status: incident.profile.session?.status ?? "unknown",
  finality: "final",
  completeness: "complete",
  analyzer_version: "fixture",
  limitations: [],
  coverage: incident.profile.coverage ?? [],
  omissions: [],
  turns: analysis.projections.turns.map((turn) => ({
    turn_id: turn.turn_id,
    metrics: turn.metrics,
    operations: incident.profile.operations
      .filter((operation) => operation.turn_id === turn.turn_id)
      .map((operation) => {
        const start = operation.started_at.monotonic_time_nano ?? "0";
        const end = operation.ended_at?.monotonic_time_nano;
        return {
          operation_id: operation.operation_id ?? undefined,
          operation_name: operation.operation_name,
          status: operation.status ?? "unknown",
          shape: end == null ? ("point" as const) : ("interval" as const),
          time_basis: "monotonic" as const,
          clock_domain_id: operation.started_at.clock_domain_id,
          start_nano: start,
          duration_nano: end == null ? null : String(BigInt(end) - BigInt(start)),
          provider:
            typeof operation.attributes?.["gen_ai.provider.name"] === "string"
              ? operation.attributes["gen_ai.provider.name"]
              : null,
          model:
            typeof operation.attributes?.["gen_ai.request.model"] === "string"
              ? operation.attributes["gen_ai.request.model"]
              : null,
          evidence: operation.evidence,
          measurements: incident.profile.quality_samples
            .filter((sample) => sample.attributes?.["earshot.turn.id"] === turn.turn_id)
            .flatMap((sample) =>
              sample.measurements
                .filter((measurement) =>
                  measurement.name.startsWith(`earshot.${operation.operation_name}.`),
                )
                .map((measurement) => ({
                  ...measurement,
                  evidence: sample.evidence,
                })),
            ),
        };
      }),
    events: incident.profile.events
      .filter((event) => event.turn_id === turn.turn_id)
      .map((event) => ({
        event_name: event.event_name,
        time_basis: "monotonic" as const,
        clock_domain_id: event.time?.clock_domain_id,
        at_nano: event.time?.monotonic_time_nano ?? "0",
        participant_id: event.participant_id,
        evidence: event.evidence,
      })),
  })),
} satisfies ExplanationLike;

describe("buildTimeline", () => {
  const timeline = buildTimeline(explanation);

  it("produces one view per analysis turn", () => {
    expect(timeline.turns).toHaveLength(5);
  });

  it("orders the pipeline stages and carries provider/model", () => {
    const [stt, llm, tts] = timeline.turns[0].stages;
    expect([stt.name, llm.name, tts.name]).toEqual(["stt", "llm", "tts"]);
    expect(stt.provider).toBe("groq");
    expect(tts.provider).toBe("cartesia");
    expect(tts.model).toBe("sonic-2");
  });

  it("renders point stages without inventing intervals to the next stage", () => {
    const [stt, llm, tts] = timeline.turns[0].stages;
    expect(stt.startMs).toBe(0);
    expect(llm.startMs).toBe(300);
    expect(tts.startMs).toBe(770);
    expect(llm.timing).toBe("point");
    expect(llm.endMs).toBeNull();
  });

  it("subtracts decimal nanoseconds before converting to Number", () => {
    const preciseExplanation = {
      bundle_id: "precise",
      session_id: "precise",
      session_status: "completed",
      finality: "final",
      completeness: "complete",
      analyzer_version: "test",
      limitations: [],
      coverage: [],
      omissions: [],
      turns: [
        {
          turn_id: "turn-precise",
          metrics: {},
          events: [],
          operations: [
            {
              operation_name: "llm",
              operation_id: "stage-10",
              status: "ok",
              shape: "point",
              time_basis: "monotonic",
              clock_domain_id: "precise-clock",
              start_nano: "18446744073709551001",
              measurements: [],
            },
            {
              operation_name: "stt",
              operation_id: "stage-2",
              status: "ok",
              shape: "point",
              time_basis: "monotonic",
              clock_domain_id: "precise-clock",
              start_nano: "18446744073709551000",
              measurements: [],
            },
          ],
        },
      ],
    } as ExplanationLike;

    const stages = buildTimeline(preciseExplanation).turns[0].stages;
    expect(stages.map((stage) => stage.name)).toEqual(["stt", "llm"]);
    expect(stages[1].startMs).toBe(0.000001);
  });

  it("preserves the analyzer's temporal turn order instead of sorting identifiers", () => {
    const source = explanation.turns[0];
    const ordered = {
      ...explanation,
      turns: [
        { ...source, turn_id: "turn-2" },
        { ...source, turn_id: "turn-10" },
      ],
    } satisfies ExplanationLike;

    expect(buildTimeline(ordered).turns.map((turn) => turn.turnId)).toEqual([
      "turn-2",
      "turn-10",
    ]);
  });

  it("marks cross-clock stage placement unavailable", () => {
    const crossClockExplanation = {
      bundle_id: "cross-clock",
      session_id: "cross-clock",
      session_status: "completed",
      finality: "final",
      completeness: "complete",
      analyzer_version: "test",
      limitations: [],
      coverage: [],
      omissions: [],
      turns: [
        {
          turn_id: "turn-cross-clock",
          metrics: {},
          events: [],
          operations: [
            {
              operation_name: "stt",
              status: "ok",
              shape: "point",
              time_basis: "monotonic",
              start_nano: "100",
              clock_domain_id: "server",
              measurements: [],
            },
            {
              operation_name: "llm",
              status: "ok",
              shape: "point",
              time_basis: "monotonic",
              start_nano: "200",
              clock_domain_id: "browser",
              measurements: [],
            },
          ],
        },
      ],
    } as ExplanationLike;

    const [, llm] = buildTimeline(crossClockExplanation).turns[0].stages;
    const [stt] = buildTimeline(crossClockExplanation).turns[0].stages;
    expect(stt.timing).toBe("unavailable");
    expect(stt.startMs).toBeNull();
    expect(llm.timing).toBe("unavailable");
    expect(llm.startMs).toBeNull();
  });

  it("surfaces the measured first-token latency, including the slow turn", () => {
    expect(timeline.turns[0].firstToken.value).toBe(240);
    expect(timeline.turns[0].firstToken.confidence).toBe("measured");
    expect(timeline.turns[3].firstToken.value).toBe(720);
  });

  it("flags the barge-in turn", () => {
    expect(timeline.turns[2].interrupted).toBe(true);
    expect(timeline.turns[0].interrupted).toBe(false);
  });

  it("computes a shared axis that covers the longest turn", () => {
    expect(timeline.scaleMs % 250).toBe(0);
    expect(timeline.scaleMs).toBeGreaterThanOrEqual(
      Math.max(...timeline.turns.map((t) => t.totalMs)),
    );
  });
});

describe("buildSummary", () => {
  const summary = buildSummary(incident, explanation, buildTimeline(explanation));

  it("counts turns and interruptions", () => {
    expect(summary.turns).toBe(5);
    expect(summary.interruptions).toBe(1);
    expect(summary.status).toBe("completed");
  });

  it("lists each provider·model in the stack once", () => {
    expect(summary.stack).toContain("cartesia · sonic-2");
    expect(summary.stack).toHaveLength(3);
  });

  it("reports a p95 first-token that reflects the slow turn", () => {
    expect(summary.p95FirstTokenMs).toBe(720);
  });

  it("keeps a missing or cross-clock session duration unavailable", () => {
    const unavailableIncident = {
      ...incident,
      profile: {
        ...incident.profile,
        session: {
          ...incident.profile.session,
          started_at: {
            monotonic_time_nano: "100",
            clock_domain_id: "server",
          },
          ended_at: {
            monotonic_time_nano: "200",
            clock_domain_id: "browser",
          },
        },
      },
    } satisfies IncidentLike;

    expect(
      buildSummary(unavailableIncident, explanation, buildTimeline(explanation))
        .durationMs,
    ).toBeNull();
  });
});

describe("buildTurnDetails", () => {
  const details = buildTurnDetails(explanation);

  it("produces one detail per turn", () => {
    expect(details).toHaveLength(5);
  });

  it("attaches provenance evidence to each stage", () => {
    const stt = details[0].stages.find((s) => s.name === "stt");
    expect(stt?.evidence?.source).toBe("app");
    expect(stt?.evidence?.observer).toBe("server");
    expect(stt?.evidence?.confidence).toBe("inferred");
    expect(stt?.status).toBe("ok");
  });

  it("collects the per-stage measurements", () => {
    const stt = details[0].stages.find((s) => s.name === "stt");
    expect(stt?.measurements.some((m) => m.name.includes("stt"))).toBe(true);
    expect(stt?.measurements[0].unit).toBe("ms");
  });

  it("exposes all derived metrics under friendly keys", () => {
    const keys = details[0].metrics.map((m) => m.key);
    expect(keys).toContain("first_token");
    expect(keys).toContain("response");
    expect(details[0].firstTokenMs).toBe(240);
    expect(details[3].firstTokenMs).toBe(720);
  });

  it("lists the turn's events with the acting participant", () => {
    const names = details[0].events.map((e) => e.name);
    expect(names).toContain("earshot.speech.ended");
    expect(details[0].events[0].participant).toBe("user");
    expect(
      details[2].events.some((e) => e.name === "earshot.interruption.accepted"),
    ).toBe(true);
  });

  it("keeps an unaligned event offset unavailable instead of coercing it to zero", () => {
    const crossClock = {
      ...explanation,
      turns: [
        {
          ...explanation.turns[0],
          operations: explanation.turns[0].operations.slice(0, 1),
          events: [
            {
              event_name: "earshot.audio.render.started",
              time_basis: "monotonic" as const,
              clock_domain_id: "browser",
              at_nano: "1720000000",
            },
          ],
        },
      ],
    } satisfies ExplanationLike;

    expect(buildTurnDetails(crossClock)[0].events[0].atMs).toBeNull();
  });
});

describe("getCoverage", () => {
  it("reports only the signals that were not fully observed", () => {
    const gaps = getCoverage(explanation);
    expect(gaps.some((g) => g.signal === "client.render")).toBe(true);
    expect(gaps.every((g) => g.availability !== "available")).toBe(true);
    expect(gaps.find((g) => g.signal === "client.render")?.reason).toContain("client");
  });
});

// -- generic operation list (WS-3) ------------------------------------------

interface RawOp {
  operation_id?: string;
  operation_name: string;
  status?: string;
  shape?: "point" | "interval";
  start_nano?: string;
  duration_nano?: string | null;
  clock?: string;
  trace_id?: string;
  span_id?: string;
  parent_span_id?: string;
  parent_scope?: string;
  measurements?: { name: string; value: boolean | number; unit: string }[];
}

/** A single-turn explanation over an arbitrary operation set, one clock. */
function turnOf(operations: RawOp[]): ExplanationLike {
  return {
    bundle_id: "generic",
    session_id: "generic",
    session_status: "completed",
    finality: "final",
    completeness: "complete",
    analyzer_version: "test",
    limitations: [],
    coverage: [],
    omissions: [],
    turns: [
      {
        turn_id: "turn-generic",
        metrics: {},
        events: [],
        operations: operations.map((raw, i) => ({
          operation_id: raw.operation_id,
          operation_name: raw.operation_name,
          status: raw.status ?? "ok",
          shape: raw.shape ?? "interval",
          time_basis: "monotonic" as const,
          clock_domain_id: raw.clock ?? "generic-clock",
          trace_id: raw.trace_id,
          span_id: raw.span_id,
          parent_span_id: raw.parent_span_id,
          parent_scope: raw.parent_scope,
          start_nano: raw.start_nano ?? String(1000 + i * 1000),
          duration_nano:
            raw.duration_nano === undefined ? "500000000" : raw.duration_nano,
          measurements: (raw.measurements ?? []).map((m) => ({ ...m })),
        })),
      },
    ],
  } satisfies ExplanationLike;
}

describe("generic operation list", () => {
  it("renders a native speech-to-speech turn (single agent op) instead of an empty timeline", () => {
    const timeline = buildTimeline(
      turnOf([{ operation_id: "op-native-agent", operation_name: "agent" }]),
    );
    const stages = timeline.turns[0].stages;
    expect(stages).toHaveLength(1);
    expect(stages[0].name).toBe("agent");
    expect(stages[0].role).toBe("agent");
    expect(stages[0].operationId).toBe("op-native-agent");
    expect(timeline.turns[0].hasCascade).toBe(false);
    // The agent op is placed, not dropped.
    expect(stages[0].timing).toBe("interval");
  });

  it("keeps a tool call and its same-named retry as distinct addressable operations", () => {
    const details = buildTurnDetails(
      turnOf([
        {
          operation_id: "op-tool-attempt-1",
          operation_name: "tool",
          status: "timeout",
          start_nano: "1000",
        },
        {
          operation_id: "op-tool-attempt-2",
          operation_name: "tool",
          status: "ok",
          start_nano: "2000",
        },
        {
          operation_id: "op-downstream-agent",
          operation_name: "agent",
          start_nano: "3000",
        },
      ]),
    );
    const tools = details[0].stages.filter((s) => s.role === "tool");
    expect(tools).toHaveLength(2);
    // Two same-named ops do not collide: each is separately keyed and addressable.
    expect(new Set(tools.map((t) => t.operationId)).size).toBe(2);
    expect(tools[0].status).toBe("timeout");
    expect(tools[1].status).toBe("ok");
  });

  it("renders transport, vad, and render operations that used to vanish", () => {
    const stages = buildTimeline(
      turnOf([
        { operation_name: "vad", start_nano: "1000" },
        { operation_name: "transport_send", start_nano: "2000" },
        { operation_name: "stt", start_nano: "3000" },
        { operation_name: "transport_receive", start_nano: "4000" },
        { operation_name: "render", start_nano: "5000" },
      ]),
    ).turns[0].stages;
    expect(stages.map((s) => s.role)).toEqual([
      "vad",
      "transport",
      "stt",
      "transport",
      "render",
    ]);
    // A fully cascaded turn no longer drops transport/render (all ops survive).
    expect(stages).toHaveLength(5);
  });

  it("assigns lead metrics only to cascade stages, not other roles", () => {
    const stages = buildTimeline(
      turnOf([
        {
          operation_name: "llm",
          start_nano: "1000",
          measurements: [{ name: "earshot.llm.ttft", value: 200, unit: "ms" }],
        },
        {
          operation_name: "tool",
          start_nano: "2000",
          measurements: [{ name: "earshot.tool.ttft", value: 999, unit: "ms" }],
        },
      ]),
    ).turns[0].stages;
    const llm = stages.find((s) => s.role === "llm");
    const tool = stages.find((s) => s.role === "tool");
    expect(llm?.leadMs).toBe(200);
    expect(tool?.leadMs).toBeNull();
  });

  it("carries each measurement's real unit through to the drawer view", () => {
    const details = buildTurnDetails(
      turnOf([
        {
          operation_name: "vad",
          measurements: [
            { name: "earshot.audio.input_level", value: -21.4, unit: "dbfs" },
            { name: "earshot.vad.active", value: true, unit: "1" },
          ],
        },
      ]),
    );
    const vad = details[0].stages.find((s) => s.role === "vad");
    const level = vad?.measurements.find((m) => m.unit === "dbfs");
    expect(level?.value).toBe(-21.4);
    // The boolean measurement is retained, not filtered out as non-numeric.
    expect(vad?.measurements.some((m) => m.value === true)).toBe(true);
  });

  it("flags a turn that contains the STT->LLM->TTS cascade", () => {
    expect(buildTimeline(explanation).turns[0].hasCascade).toBe(true);
    const noCascade = buildTimeline(turnOf([{ operation_name: "agent" }])).turns[0]
      .hasCascade;
    expect(noCascade).toBe(false);
  });
});

// -- real fault-family projections (WS-5) -----------------------------------
// These are the actual backend explanation projections, decoded and dumped from
// the fault fixtures, so the transform is exercised against real link/diagnosis/
// error/unassigned shapes rather than hand-built stand-ins.

describe("causal edges from links", () => {
  it("renders an observed in-trace parent edge", () => {
    const [detail] = buildTurnDetails(
      turnOf([
        {
          operation_id: "op-parent",
          operation_name: "agent",
          trace_id: "a".repeat(32),
          span_id: "1".repeat(16),
        },
        {
          operation_id: "op-child",
          operation_name: "tool",
          trace_id: "a".repeat(32),
          span_id: "2".repeat(16),
          parent_span_id: "1".repeat(16),
          parent_scope: "internal",
        },
      ]),
    );

    expect(detail.edges).toContainEqual({
      fromOperationId: "op-parent",
      toOperationId: "op-child",
      relationship: "parent",
    });
  });

  it("resolves the retry and consume edges in tool_timeout_retry", () => {
    const detail = buildTurnDetails(asExplanation(toolTimeoutRetry))[0];
    expect(detail.edges).toEqual([
      {
        fromOperationId: "op-tool-attempt-2",
        toOperationId: "op-tool-attempt-1",
        relationship: "retries",
      },
      {
        fromOperationId: "op-downstream-agent",
        toOperationId: "op-tool-attempt-2",
        relationship: "consumes",
      },
    ]);
  });

  it("resolves the handoff edge in telephony_handoff", () => {
    const detail = buildTurnDetails(asExplanation(telephonyHandoff))[0];
    const handoff = detail.edges.find((e) => e.relationship === "handoff");
    expect(handoff).toEqual({
      fromOperationId: "op-human-leg",
      toOperationId: "op-bot-leg",
      relationship: "handoff",
    });
  });

  it("resolves the duplicates and supersedes edges in websocket_reconnect", () => {
    const detail = buildTurnDetails(asExplanation(websocketReconnect))[0];
    const rels = detail.edges.map((e) => e.relationship).sort();
    expect(rels).toContain("duplicates");
    expect(rels).toContain("supersedes");
    const dup = detail.edges.find((e) => e.relationship === "duplicates");
    expect(dup?.toOperationId).toBe("op-ws-message-original");
  });

  it("never invents an edge for an operation with no links", () => {
    const detail = buildTurnDetails(asExplanation(nativeInterruption))[0];
    expect(detail.edges).toEqual([]);
  });
});

describe("per-operation error / status", () => {
  it("flags the timed-out tool attempt as abnormal with a warn tone", () => {
    const detail = buildTurnDetails(asExplanation(toolTimeoutRetry))[0];
    const attempt1 = detail.stages.find((s) => s.operationId === "op-tool-attempt-1");
    expect(attempt1?.status).toBe("timeout");
    expect(attempt1?.statusView.abnormal).toBe(true);
    expect(attempt1?.statusView.tone).toBe("warn");
    expect(attempt1?.statusView.label).toBe("timeout");
    // The succeeded retry is not badged.
    const attempt2 = detail.stages.find((s) => s.operationId === "op-tool-attempt-2");
    expect(attempt2?.statusView.abnormal).toBe(false);
  });

  it("surfaces an explicit error object as a crit badge with code and category", () => {
    const withError = asExplanation({
      bundle_id: "e",
      session_id: "e",
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
          events: [],
          operations: [
            {
              operation_id: "op-tool",
              operation_name: "tool",
              status: "failed",
              shape: "point",
              time_basis: "monotonic",
              clock_domain_id: "c",
              start_nano: "1000",
              error: {
                code: "tool_timeout",
                category: "timeout",
                capture_class: "metadata",
              },
              measurements: [],
            },
          ],
        },
      ],
    });
    const stage = buildTurnDetails(withError)[0].stages[0];
    expect(stage.statusView.tone).toBe("crit");
    expect(stage.statusView.label).toBe("tool_timeout · timeout");
    expect(stage.statusView.error).toEqual({
      code: "tool_timeout",
      category: "timeout",
      captureClass: "metadata",
    });
  });
});

describe("diagnoses", () => {
  it("surfaces the operation.failed diagnosis with its evidence operation and turn", () => {
    const diagnoses = buildDiagnoses(asExplanation(toolTimeoutRetry));
    expect(diagnoses).toHaveLength(1);
    const diag = diagnoses[0];
    expect(diag.code).toBe("operation.failed");
    expect(diag.confidence).toBe("measured");
    expect(diag.evidence).toEqual([{ id: "op-tool-attempt-1", turnIndex: 0 }]);
  });

  it("returns no diagnoses when the explanation carries none", () => {
    expect(buildDiagnoses(asExplanation(nativeInterruption))).toEqual([]);
  });
});

describe("unassigned session-level facts", () => {
  it("renders webrtc jitter/rtt/packet-loss as unassigned measurements with units", () => {
    const explanation = asExplanation(webrtcDegradation);
    // The incident has no turns; without the unassigned lane the inspector would
    // be empty.
    expect(buildTimeline(explanation).turns).toHaveLength(0);
    const facts = buildUnassigned(explanation);
    const byName = new Map(facts.measurements.map((m) => [m.name, m]));
    expect(byName.get("jitter")?.unit).toBe("ms");
    expect(byName.get("jitter")?.value).toBe(42);
    expect(byName.get("round_trip_time")?.unit).toBe("ms");
    expect(byName.get("packet_loss_ratio")?.unit).toBe("1");
    expect(byName.get("packet_loss_ratio")?.value).toBe(0.18);
  });

  it("has no unassigned facts for a fully turn-scoped incident", () => {
    const facts = buildUnassigned(asExplanation(toolTimeoutRetry));
    expect(facts.operations).toEqual([]);
    expect(facts.measurements).toEqual([]);
  });
});

describe("interruption attachment", () => {
  it("keeps a stream-less interruption event turn-level, attached to no operation", () => {
    const detail = buildTurnDetails(asExplanation(nativeInterruption))[0];
    const interruption = detail.events.find((e) => e.name.includes("interruption"));
    expect(interruption).toBeDefined();
    expect(interruption?.attachedOperationId).toBeNull();
    expect(detail.stages.every((s) => s.interruptedByEvent == null)).toBe(true);
  });
});
