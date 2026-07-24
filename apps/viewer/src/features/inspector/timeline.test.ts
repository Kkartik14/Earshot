import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import toolTimeoutRetry from "./__fixtures__/faults/tool_timeout_retry.explanation.json";
import telephonyHandoff from "./__fixtures__/faults/telephony_handoff.explanation.json";
import websocketReconnect from "./__fixtures__/faults/websocket_reconnect.explanation.json";
import webrtcDegradation from "./__fixtures__/faults/webrtc_degradation.explanation.json";
import nativeInterruption from "./__fixtures__/faults/native_s2s_interruption.explanation.json";
import bargeIn from "./__fixtures__/faults/barge_in.explanation.json";
import fullBargeInChain from "./__fixtures__/faults/full_barge_in_chain.explanation.json";
import falseInterruption from "./__fixtures__/faults/false_interruption.explanation.json";
import deviceUnavailable from "./__fixtures__/faults/device_unavailable.explanation.json";
import fastEndpointing from "./__fixtures__/faults/fast_endpointing.explanation.json";
import llmDelay from "./__fixtures__/faults/llm_delay.explanation.json";
import slowEndpointing from "./__fixtures__/faults/slow_endpointing.explanation.json";
import sttDelay from "./__fixtures__/faults/stt_delay.explanation.json";
import privacyOptOut from "./__fixtures__/faults/privacy_opt_out.explanation.json";
import ttsDelay from "./__fixtures__/faults/tts_delay.explanation.json";
import {
  buildClockCalibration,
  buildContradictions,
  buildDiagnoses,
  buildMediaCustody,
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  buildUnassigned,
  clockComparability,
  getCoverage,
  operationStatus,
  type AnalysisLike,
  type ContradictionsLike,
  type ExplanationLike,
  type IncidentLike,
} from "./timeline";

const asExplanation = (fixture: unknown): ExplanationLike =>
  fixture as unknown as ExplanationLike;

const incident = incidentFixture as unknown as IncidentLike;
const analysis = analysisFixture as unknown as AnalysisLike;
const explanation = asExplanation({
  bundle_id: "fixture-bundle",
  session_id: "fixture-session",
  session_status: incident.profile.session?.status ?? "unknown",
  finality: "final",
  completeness: "complete",
  analyzer_version: "fixture",
  limitations: [],
  coverage: incident.profile.coverage ?? [],
  omissions: [],
  turns: analysis.projections!.turns.map((turn) => ({
    turn_id: turn.turn_id,
    metrics: turn.metrics,
    operations: incident.profile.operations
      .filter((operation) => operation.turn_id === turn.turn_id)
      .map((operation, operationIndex) => {
        const start = operation.started_at.monotonic_time_nano ?? "0";
        const end = operation.ended_at?.monotonic_time_nano;
        return {
          operation_id: operation.operation_id ?? `fixture-operation-${operationIndex}`,
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
});

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
    const preciseExplanation = asExplanation({
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
    });

    const stages = buildTimeline(preciseExplanation).turns[0].stages;
    expect(stages.map((stage) => stage.name)).toEqual(["stt", "llm"]);
    expect(stages[1].startMs).toBe(0.000001);
  });

  it("preserves the analyzer's temporal turn order instead of sorting identifiers", () => {
    const source = explanation.turns[0];
    const ordered = asExplanation({
      ...explanation,
      turns: [
        { ...source, turn_id: "turn-2" },
        { ...source, turn_id: "turn-10" },
      ],
    });

    expect(buildTimeline(ordered).turns.map((turn) => turn.turnId)).toEqual([
      "turn-2",
      "turn-10",
    ]);
  });

  it("marks cross-clock stage placement unavailable", () => {
    const crossClockExplanation = asExplanation({
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
    });

    const [crossClockTurn] = buildTimeline(crossClockExplanation).turns;
    const [stt, llm] = crossClockTurn.stages;
    expect(stt.timing).toBe("unavailable");
    expect(stt.startMs).toBeNull();
    expect(llm.timing).toBe("unavailable");
    expect(llm.startMs).toBeNull();
    expect(crossClockTurn.totalMs).toBeNull();
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
    const knownDurations = timeline.turns
      .map((turn) => turn.totalMs)
      .filter((value): value is number => value != null);
    expect(timeline.scaleMs % 250).toBe(0);
    expect(timeline.scaleMs).toBeGreaterThanOrEqual(Math.max(...knownDurations));
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
            observed_time_unix_nano: null,
            source_time_unix_nano: null,
            uncertainty_nano: null,
          },
          ended_at: {
            monotonic_time_nano: "200",
            clock_domain_id: "browser",
            observed_time_unix_nano: null,
            source_time_unix_nano: null,
            uncertainty_nano: null,
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

  it("retains repeated exact turn measurements with authored provenance", () => {
    const withTurnFacts = asExplanation({
      ...explanation,
      turns: [
        {
          ...explanation.turns[0],
          measurements: [
            {
              name: "provider.queue_depth",
              value: 10,
              unit: "{item}",
              aggregation: "instant",
              basis: "provider_measurement",
              confidence: "measured",
              evidence: { source_field: "queue.depth.first" },
              evidence_ids: ["quality-turn-1"],
            },
            {
              name: "provider.queue_depth",
              value: 20,
              unit: "{item}",
              aggregation: "instant",
              basis: "provider_measurement",
              confidence: "measured",
              evidence: { source_field: "queue.depth.second" },
              evidence_ids: ["quality-turn-2"],
            },
          ],
        },
      ],
    });

    const [detail] = buildTurnDetails(withTurnFacts);

    expect(detail.measurements.map((measurement) => measurement.value)).toEqual([10, 20]);
    expect(detail.measurements.map((measurement) => measurement.evidenceIds)).toEqual([
      ["quality-turn-1"],
      ["quality-turn-2"],
    ]);
    expect(detail.measurements.map((measurement) => measurement.sourceField)).toEqual([
      "queue.depth.first",
      "queue.depth.second",
    ]);
    expect(
      detail.stages.flatMap((stage) => stage.measurements).map((item) => item.name),
    ).not.toContain("provider.queue_depth");
    expect(buildUnassigned(withTurnFacts).measurements).toEqual([]);
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
    const crossClock = asExplanation({
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
    });

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
  stream_id?: string;
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
  return asExplanation({
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
          operation_id: raw.operation_id ?? `op-${i}`,
          operation_name: raw.operation_name,
          stream_id: raw.stream_id,
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
  });
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

  it("converts a lead measurement to milliseconds using its declared unit", () => {
    const stages = buildTimeline(
      turnOf([
        {
          operation_name: "llm",
          start_nano: "1000",
          // Producers that report the lead in SECONDS (e.g. Pipecat metrics.ttfb,
          // LiveKit *_latency, both unit="s") must not be read as raw ms.
          measurements: [{ name: "earshot.llm.ttft", value: 2, unit: "s" }],
        },
        {
          operation_name: "tts",
          start_nano: "2000",
          measurements: [{ name: "earshot.tts.ttfb", value: 90, unit: "ms" }],
        },
      ]),
    ).turns[0].stages;
    const llm = stages.find((s) => s.role === "llm");
    const tts = stages.find((s) => s.role === "tts");
    // 2 s -> 2000 ms, not 2 ms.
    expect(llm?.leadMs).toBe(2000);
    // A milliseconds-unit lead passes through unchanged.
    expect(tts?.leadMs).toBe(90);
  });

  it("leaves a lead measurement in an unconvertible unit unplaced instead of mislabeling it as ms", () => {
    const stages = buildTimeline(
      turnOf([
        {
          operation_name: "stt",
          start_nano: "1000",
          measurements: [{ name: "earshot.stt.ttfb", value: 5, unit: "{frame}" }],
        },
      ]),
    ).turns[0].stages;
    expect(stages.find((s) => s.role === "stt")?.leadMs).toBeNull();
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
  it("keeps an explicitly external link outside the local call graph", () => {
    const source = turnOf([
      { operation_id: "op-source", operation_name: "agent" },
      { operation_id: "op-local-collision", operation_name: "tool" },
    ]);
    const [turn] = source.turns;
    const [sourceOperation, localCollision] = turn.operations;
    const [detail] = buildTurnDetails(
      asExplanation({
        ...source,
        turns: [
          {
            ...turn,
            operations: [
              {
                ...sourceOperation,
                links: [
                  {
                    relationship: "consumes",
                    target_scope: "external",
                    target_operation_id: "op-local-collision",
                  },
                ],
              },
              localCollision,
            ],
          },
        ],
      }),
    );

    expect(detail.edges).toEqual([]);
    expect(detail.stages[0].links).toEqual([
      {
        relationship: "consumes",
        targetOperationId: "op-local-collision",
        targetScope: "external",
        resolved: false,
      },
    ]);
  });

  it("resolves an internal trace/span-only link to its observed operation", () => {
    const traceId = "a".repeat(32);
    const targetSpanId = "1".repeat(16);
    const source = turnOf([
      {
        operation_id: "op-source",
        operation_name: "agent",
        trace_id: traceId,
        span_id: "2".repeat(16),
      },
      {
        operation_id: "op-target",
        operation_name: "tool",
        trace_id: traceId,
        span_id: targetSpanId,
      },
    ]);
    const [turn] = source.turns;
    const [sourceOperation, targetOperation] = turn.operations;
    const [detail] = buildTurnDetails(
      asExplanation({
        ...source,
        turns: [
          {
            ...turn,
            operations: [
              {
                ...sourceOperation,
                links: [
                  {
                    relationship: "consumes",
                    target_scope: "internal",
                    trace_id: traceId,
                    span_id: targetSpanId,
                  },
                ],
              },
              targetOperation,
            ],
          },
        ],
      }),
    );

    expect(detail.edges).toEqual([
      {
        fromOperationId: "op-source",
        toOperationId: "op-target",
        relationship: "consumes",
      },
    ]);
    expect(detail.stages[0].links[0]).toEqual({
      relationship: "consumes",
      targetOperationId: "op-target",
      targetScope: "internal",
      resolved: true,
    });
  });

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
  it.each(["unset", "unknown"])(
    "keeps neutral %s status from becoming an abnormal badge",
    (status) => {
      expect(operationStatus({ status })).toEqual({
        abnormal: false,
        tone: "muted",
        label: status,
        error: undefined,
      });
    },
  );

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
    // The analyzer now also attributes the retry pattern (tool.retry) alongside
    // the raw operation.failed fact; both cite the timed-out tool operation.
    const diag = diagnoses.find((d) => d.code === "operation.failed");
    expect(diag).toBeDefined();
    expect(diag!.confidence).toBe("measured");
    expect(diag!.evidence).toEqual([{ id: "op-tool-attempt-1", turnIndex: 0 }]);
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
  it("attaches an event only to its explicit operation identity", () => {
    const source = turnOf([
      {
        operation_id: "op-target",
        operation_name: "agent",
        stream_id: "shared-stream",
      },
      {
        operation_id: "op-other",
        operation_name: "transport_send",
        stream_id: "shared-stream",
      },
    ]);
    const [turn] = source.turns;
    const explanation = asExplanation({
      ...source,
      turns: [
        {
          ...turn,
          events: [
            {
              event_id: "evt-interruption",
              event_name: "earshot.interruption.accepted",
              operation_id: "op-target",
              stream_id: "shared-stream",
              time_basis: "monotonic",
              clock_domain_id: "generic-clock",
              at_nano: "1500",
              evidence_ids: ["evt-interruption"],
            },
          ],
        },
      ],
    });

    const [detail] = buildTurnDetails(explanation);

    expect(detail.events[0].attachedOperationId).toBe("op-target");
    expect(
      detail.stages.find((stage) => stage.operationId === "op-target")
        ?.interruptedByEvent,
    ).toBe("earshot.interruption.accepted");
    expect(
      detail.stages.find((stage) => stage.operationId === "op-other")?.interruptedByEvent,
    ).toBeUndefined();
  });

  it("keeps a stream-only event turn-level instead of inferring causality", () => {
    const source = turnOf([
      {
        operation_id: "op-agent",
        operation_name: "agent",
        stream_id: "stream-output",
      },
    ]);
    const [turn] = source.turns;
    const explanation = asExplanation({
      ...source,
      turns: [
        {
          ...turn,
          events: [
            {
              event_id: "evt-interruption",
              event_name: "earshot.interruption.accepted",
              stream_id: "stream-output",
              time_basis: "monotonic",
              clock_domain_id: "generic-clock",
              at_nano: "1500",
              evidence_ids: ["evt-interruption"],
            },
          ],
        },
      ],
    });

    const [detail] = buildTurnDetails(explanation);

    expect(detail.events[0].attachedOperationId).toBeNull();
    expect(detail.stages[0].interruptedByEvent).toBeUndefined();
  });

  it("does not relabel detected or ignored interruption evidence as accepted", () => {
    const source = turnOf([{ operation_id: "op-agent", operation_name: "agent" }]);
    const [turn] = source.turns;
    const events = ["detected", "ignored"].map((outcome, index) => ({
      event_id: `evt-interruption-${outcome}`,
      event_name: `earshot.interruption.${outcome}`,
      operation_id: "op-agent",
      time_basis: "monotonic" as const,
      clock_domain_id: "generic-clock",
      at_nano: String(1500 + index),
      evidence_ids: [`evt-interruption-${outcome}`],
    }));
    const [detail] = buildTurnDetails(
      asExplanation({
        ...source,
        turns: [{ ...turn, events }],
      }),
    );

    expect(detail.interrupted).toBe(false);
    expect(detail.events.map((event) => event.attachedOperationId)).toEqual([
      "op-agent",
      "op-agent",
    ]);
    expect(detail.stages[0].interruptedByEvent).toBeUndefined();
  });

  it("keeps a stream-less interruption event turn-level, attached to no operation", () => {
    const detail = buildTurnDetails(asExplanation(nativeInterruption))[0];
    const interruption = detail.events.find((e) => e.name.includes("interruption"));
    expect(interruption).toBeDefined();
    expect(interruption?.attachedOperationId).toBeNull();
    expect(detail.stages.every((s) => s.interruptedByEvent == null)).toBe(true);
  });
});

describe("remaining fault-family projections", () => {
  it("uses backend-authored operation identity for telephony, retry, and reconnect events", () => {
    const telephony = buildTurnDetails(asExplanation(telephonyHandoff))[0];
    expect(
      telephony.events.map((event) => [event.name, event.attachedOperationId]),
    ).toEqual([
      ["earshot.telephony.dtmf.received", "op-inbound-leg"],
      ["earshot.telephony.voicemail.detected", "op-bot-leg"],
    ]);

    const retry = buildTurnDetails(asExplanation(toolTimeoutRetry))[0];
    expect(retry.events).toContainEqual(
      expect.objectContaining({
        name: "earshot.tool.retry.downstream_resumed",
        attachedOperationId: "op-downstream-agent",
      }),
    );

    const reconnect = buildTurnDetails(asExplanation(websocketReconnect))[0];
    expect(
      reconnect.events.map((event) => [event.name, event.attachedOperationId]),
    ).toEqual([
      ["earshot.transport.reconnecting", null],
      ["earshot.transport.message.duplicate", "op-ws-message-duplicate"],
      ["earshot.transport.message.out_of_order", "op-ws-message-out-of-order"],
    ]);
  });

  it("renders the observed barge-in cancellations and their explicit event owners", () => {
    const [detail] = buildTurnDetails(asExplanation(bargeIn));

    expect(detail.stages.map((stage) => [stage.operationId, stage.status])).toEqual([
      ["op-agent", "cancelled"],
      ["op-tts", "cancelled"],
      ["op-render", "cancelled"],
    ]);
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.interruption.detected", null],
        ["earshot.interruption.accepted", null],
        ["earshot.model.cancelled", "op-agent"],
        ["earshot.audio.queued.discarded", "op-tts"],
        ["earshot.audio.render.stopped", "op-render"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("keeps unavailable-device evidence as events and backend-authored coverage", () => {
    const explanation = asExplanation(deviceUnavailable);
    const [detail] = buildTurnDetails(explanation);

    expect(detail.stages).toEqual([]);
    expect(detail.events.map((event) => event.name)).toEqual([
      "earshot.device.permission_denied",
      "earshot.device.audio_context_suspended",
    ]);
    expect(
      getCoverage(explanation)
        .filter((row) => row.availability === "not_observed")
        .map((row) => [row.signal, row.reason]),
    ).toEqual([
      ["capture", "device_unavailable"],
      ["client.render", "app_backgrounded"],
      ["device.microphone", "permission_denied"],
    ]);
  });

  it("renders fast endpointing from the observed VAD and commit boundaries", () => {
    const [detail] = buildTurnDetails(asExplanation(fastEndpointing));

    expect(
      detail.stages.map((stage) => [stage.operationId, stage.startMs, stage.endMs]),
    ).toEqual([
      ["op-vad", 0, 200],
      ["op-turn", 200, 280],
    ]);
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.speech.ended", "op-vad"],
        ["earshot.turn.committed", "op-turn"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("renders slow endpointing without relabeling its long observed interval", () => {
    const [detail] = buildTurnDetails(asExplanation(slowEndpointing));

    expect(
      detail.stages.map((stage) => [stage.operationId, stage.startMs, stage.endMs]),
    ).toEqual([
      ["op-vad", 0, 200],
      ["op-turn", 200, 1500],
    ]);
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.speech.ended", "op-vad"],
        ["earshot.turn.committed", "op-turn"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("keeps the observed LLM delay and all downstream response boundaries visible", () => {
    const [detail] = buildTurnDetails(asExplanation(llmDelay));

    expect(detail.stages.map((stage) => stage.operationId)).toEqual([
      "op-stt",
      "op-llm",
      "op-tts",
      "op-send",
      "op-receive",
      "op-render",
    ]);
    expect(detail.stages.find((stage) => stage.operationId === "op-llm")).toMatchObject({
      startMs: 300,
      endMs: 2500,
      timing: "interval",
    });
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.response.first_token", "op-llm"],
        ["earshot.response.first_audio_generated", "op-tts"],
        ["earshot.audio.first_byte_sent", "op-send"],
        ["earshot.audio.first_packet_received", "op-receive"],
        ["earshot.audio.render.started", "op-render"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("keeps the observed STT delay and all downstream response boundaries visible", () => {
    const [detail] = buildTurnDetails(asExplanation(sttDelay));

    expect(detail.stages.map((stage) => stage.operationId)).toEqual([
      "op-stt",
      "op-llm",
      "op-tts",
      "op-send",
      "op-receive",
      "op-render",
    ]);
    expect(detail.stages.find((stage) => stage.operationId === "op-stt")).toMatchObject({
      startMs: 0,
      endMs: 2100,
      timing: "interval",
    });
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.response.first_token", "op-llm"],
        ["earshot.response.first_audio_generated", "op-tts"],
        ["earshot.audio.first_byte_sent", "op-send"],
        ["earshot.audio.first_packet_received", "op-receive"],
        ["earshot.audio.render.started", "op-render"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("keeps the observed TTS delay and all downstream response boundaries visible", () => {
    const [detail] = buildTurnDetails(asExplanation(ttsDelay));

    expect(detail.stages.map((stage) => stage.operationId)).toEqual([
      "op-stt",
      "op-llm",
      "op-tts",
      "op-send",
      "op-receive",
      "op-render",
    ]);
    expect(detail.stages.find((stage) => stage.operationId === "op-tts")).toMatchObject({
      startMs: 600,
      endMs: 3100,
      timing: "interval",
    });
    expect(detail.events.map((event) => [event.name, event.attachedOperationId])).toEqual(
      [
        ["earshot.response.first_token", "op-llm"],
        ["earshot.response.first_audio_generated", "op-tts"],
        ["earshot.audio.first_byte_sent", "op-send"],
        ["earshot.audio.first_packet_received", "op-receive"],
        ["earshot.audio.render.started", "op-render"],
      ],
    );
    expect(detail.edges).toEqual([]);
  });

  it("renders privacy opt-out as an omission beside the retained metadata operation", () => {
    const explanation = asExplanation(privacyOptOut);
    const [detail] = buildTurnDetails(explanation);

    expect(detail.stages.map((stage) => stage.operationId)).toEqual(["op-metadata-only"]);
    expect(detail.events).toEqual([]);
    expect(detail.edges).toEqual([]);
    expect(getCoverage(explanation)).toContainEqual({
      signal: "privacy.transcript",
      availability: "omitted",
      reason: "capture_class_disabled",
    });
  });
});

describe("interruption chains", () => {
  it("projects every observed stage of a barge-in with its offset from the turn origin", () => {
    const [detail] = buildTurnDetails(asExplanation(fullBargeInChain));
    const [chain] = detail.interruptionChains;

    expect(detail.interruptionChains).toHaveLength(1);
    expect(chain.classification).toBe("accepted");
    // The full canonical vocabulary, in the analyzer's causal order.
    expect(chain.stages.map((stage) => stage.stage)).toEqual([
      "overlap_observed",
      "intent",
      "classified",
      "cancellation_requested",
      "generation_stopped",
      "queued_audio_discarded",
      "transport_stopped",
      "buffers_purged",
      "render_stopped",
      "resumed",
      "tool_outcome",
    ]);
    expect(chain.stages.every((stage) => stage.observed)).toBe(true);
    const overlap = chain.stages[0];
    const renderStopped = chain.stages.find((stage) => stage.stage === "render_stopped");
    expect(overlap.evidenceId).toBe("event-overlap");
    expect(renderStopped?.atMs).toBe((overlap.atMs as number) + 100);
    expect(chain.effectiveness).toMatchObject({
      value: 100,
      availability: "available",
      confidence: "measured",
      limitation: null,
    });
  });

  it("keeps an unobserved stage as a coverage reason rather than a zero coordinate", () => {
    const [detail] = buildTurnDetails(asExplanation(bargeIn));
    const [chain] = detail.interruptionChains;
    const missing = chain.stages.filter((stage) => !stage.observed);

    expect(missing.map((stage) => stage.stage)).toEqual([
      "intent",
      "transport_stopped",
      "buffers_purged",
      "resumed",
      "tool_outcome",
    ]);
    // No coordinate, no evidence, and an explicit reason: absence is coverage.
    expect(missing.every((stage) => stage.atMs === null)).toBe(true);
    expect(missing.every((stage) => stage.coordinate === null)).toBe(true);
    expect(missing.every((stage) => stage.evidenceId === null)).toBe(true);
    expect(missing.map((stage) => stage.coverageReason)).toEqual([
      "stage_not_observed",
      "stage_not_observed",
      "stage_not_observed",
      "stage_not_observed",
      "no_tool_in_turn",
    ]);
  });

  it("reports an underivable effectiveness as unavailable with the analyzer's limitation", () => {
    const [detected] = buildTurnDetails(asExplanation(falseInterruption));
    const [native] = buildTurnDetails(asExplanation(nativeInterruption));

    expect(detected.interruptionChains[0]).toMatchObject({
      classification: "false",
      effectiveness: {
        value: null,
        availability: "not_observed",
        limitation: "target_signal_not_observed",
      },
    });
    expect(native.interruptionChains[0].effectiveness).toMatchObject({
      value: null,
      availability: "not_observed",
      limitation: "turn_anchor_not_observed",
    });
  });

  it("keeps a stage coordinate that cannot be placed on the turn axis", () => {
    const explanation = asExplanation({
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
              shape: "point",
              time_basis: "monotonic",
              clock_domain_id: "server-clock",
              start_nano: "1000000000",
              measurements: [],
            },
          ],
          events: [],
          interruption_chains: [
            {
              turn_id: "turn-1",
              classification: "accepted",
              stages: [
                {
                  stage: "overlap_observed",
                  observed: true,
                  at_nano: "1200000000",
                  clock_domain_id: "server-clock",
                  time_basis: "monotonic",
                  evidence_id: "evt-overlap",
                },
                {
                  stage: "render_stopped",
                  observed: true,
                  at_nano: "1750000000000000000",
                  clock_domain_id: "device-clock",
                  time_basis: "source_wall",
                  evidence_id: "evt-render-stopped",
                },
              ],
              effectiveness: {
                availability: "not_observed",
                basis: "interruption_barge_in",
                confidence: "unavailable",
                limitation: "cross_clock_domain",
              },
            },
          ],
        },
      ],
    });

    const [chain] = buildTurnDetails(explanation)[0].interruptionChains;

    expect(chain.stages[0].atMs).toBe(200);
    // The device-clock stage cannot be offset against a server-clock origin, so
    // the exact recorded coordinate is kept instead of an invented +0.
    expect(chain.stages[1].atMs).toBeNull();
    expect(chain.stages[1].coordinate).toBe(
      "device-clock · source_wall · 1750000000000000000ns",
    );
  });
});

describe("clockComparability", () => {
  it("names a value estimated through a declared calibration", () => {
    expect(
      clockComparability({
        availability: "available",
        basis: "cross_clock_calibrated",
        limitation: null,
      }),
    ).toEqual({
      state: "estimated",
      note: "estimated across clock domains through a declared calibration",
    });
  });

  it("says nothing about a same-domain measurement", () => {
    expect(
      clockComparability({
        availability: "available",
        basis: "monotonic",
        limitation: null,
      }),
    ).toBeNull();
  });

  it("explains every clock-related absence and stays silent on unrelated ones", () => {
    expect(
      clockComparability({
        availability: "not_observed",
        basis: "clock_domain",
        limitation: "cross_clock_domain",
      }),
    ).toEqual({
      state: "unavailable",
      note: "two clock domains with no declared calibration between them",
    });
    expect(
      clockComparability({
        availability: "unavailable",
        basis: "clock_domain",
        limitation: "cross_clock_ambiguous",
      })?.note,
    ).toMatch(/disagree beyond their uncertainty/);
    // An absence with a non-clock cause is not attributed to the clocks.
    expect(
      clockComparability({
        availability: "not_observed",
        basis: "interruption_barge_in",
        limitation: "target_signal_not_observed",
      }),
    ).toBeNull();
  });
});

describe("buildClockCalibration", () => {
  const twoDomainIncident = {
    profile: {
      clock_domains: [
        {
          clock_domain_id: "server-clock",
          kind: "process_monotonic",
          observer: "server",
        },
        {
          clock_domain_id: "device-clock",
          kind: "device_wall",
          observer: "browser",
          uncertainty_nano: "2000000",
        },
      ],
      clock_relations: [
        {
          relation_id: "rel-1",
          from_clock_domain_id: "device-clock",
          to_clock_domain_id: "server-clock",
          method: "ntp_offset",
          offset_nano: "5000000",
          uncertainty_nano: "3000000",
        },
      ],
    },
  } as unknown as IncidentLike;

  const details = [
    {
      turnId: "turn-1",
      index: 0,
      metrics: [
        {
          key: "response",
          value: 480,
          availability: "available",
          basis: "cross_clock_calibrated",
          confidence: "estimated",
          limitation: null,
        },
        {
          key: "render_start",
          value: null,
          availability: "not_observed",
          basis: "clock_domain",
          confidence: "unavailable",
          limitation: "cross_clock_domain",
        },
        {
          key: "first_token",
          value: 150,
          availability: "available",
          basis: "monotonic",
          confidence: "measured",
          limitation: null,
        },
      ],
      interruptionChains: [
        {
          reactKey: "turn-1:0",
          turnId: "turn-1",
          classification: "accepted",
          stages: [],
          effectiveness: {
            key: "effectiveness",
            value: null,
            availability: "not_observed",
            basis: "clock_domain",
            confidence: "unavailable",
            limitation: "cross_clock_domain",
          },
        },
      ],
    },
  ] as unknown as ReturnType<typeof buildTurnDetails>;

  it("carries each declared calibration's own uncertainty", () => {
    const { domains, relations } = buildClockCalibration(twoDomainIncident, []);

    expect(domains.map((domain) => domain.id)).toEqual(["server-clock", "device-clock"]);
    // A domain that declares no error bound reports null — unknown, not zero.
    expect(domains[0].uncertaintyMs).toBeNull();
    expect(domains[1].uncertaintyMs).toBe(2);
    expect(relations).toEqual([
      {
        relationId: "rel-1",
        fromDomain: "device-clock",
        toDomain: "server-clock",
        method: "ntp_offset",
        uncertaintyMs: 3,
        driftPpm: null,
      },
    ]);
  });

  it("lists every latency the clocks decide, and only those", () => {
    const { crossClock } = buildClockCalibration(twoDomainIncident, details);

    expect(crossClock.map((row) => [row.metric, row.state, row.availability])).toEqual([
      ["response", "estimated", "available"],
      ["render_start", "unavailable", "not_observed"],
      ["interruption 0 · effectiveness", "unavailable", "not_observed"],
    ]);
    // A same-domain measured latency is not a cross-clock story.
    expect(crossClock.some((row) => row.metric === "first_token")).toBe(false);
    expect(crossClock[1].note).toMatch(/no declared calibration/);
  });

  it("reports no clock story for a single-domain session", () => {
    const single = {
      profile: { clock_domains: [{ clock_domain_id: "c", kind: "k", observer: "o" }] },
    } as unknown as IncidentLike;

    expect(buildClockCalibration(single, [])).toEqual({
      domains: [{ id: "c", kind: "k", observer: "o", uncertaintyMs: null }],
      relations: [],
      crossClock: [],
    });
  });
});

describe("buildContradictions", () => {
  it("resolves cited operations to their turn and leaves other evidence inert", () => {
    const report = {
      bundle_id: "b",
      analyzer_version: "0.5.0",
      input_digest: "a".repeat(64),
      contradictions: [
        {
          kind: "render_claim_conflict",
          summary: "render_observed_while_coverage_not_observed",
          evidence_ids: ["op-tool-attempt-1", "quality-sample-9"],
          boundary: "render",
          turn_id: "turn-1",
          subject: "turn-1",
        },
      ],
    } as unknown as ContradictionsLike;

    const [contradiction] = buildContradictions(asExplanation(toolTimeoutRetry), report);

    expect(contradiction).toMatchObject({
      kind: "render_claim_conflict",
      boundary: "render",
      turnId: "turn-1",
    });
    expect(contradiction.evidence).toEqual([
      { id: "op-tool-attempt-1", turnIndex: 0 },
      { id: "quality-sample-9", turnIndex: null },
    ]);
  });

  it("projects an examined incident with no conflicts as an empty list", () => {
    const report = {
      bundle_id: "b",
      analyzer_version: "0.5.0",
      input_digest: "a".repeat(64),
      contradictions: [],
    } as unknown as ContradictionsLike;

    expect(buildContradictions(asExplanation(toolTimeoutRetry), report)).toEqual([]);
  });
});

describe("buildMediaCustody", () => {
  // A session whose own evidence lives in "server-clock", plus a media file with
  // its own timeline. Everything about alignment is decided from these records.
  const custodyIncident = (
    media: Record<string, unknown>,
    relations: Record<string, unknown>[] = [],
  ) =>
    ({
      profile: {
        clock_domains: [
          { clock_domain_id: "server-clock", kind: "process_monotonic", observer: "sdk" },
          { clock_domain_id: "media-1", kind: "media_timeline", observer: "vapi" },
        ],
        clock_relations: relations,
        operations: [
          {
            operation_id: "op",
            operation_name: "llm",
            status: "ok",
            started_at: { clock_domain_id: "server-clock", monotonic_time_nano: "0" },
          },
        ],
        events: [],
        media_refs: [media],
      },
    }) as unknown as IncidentLike;

  const opaque = {
    media_id: "media-1",
    session_id: "s",
    stream_id: "stream-out",
    media_kind: "audio",
    content_type: "audio/wav",
    integrity: "opaque_handle",
    custodian: "provider.vapi",
    clock_domain_id: "media-1",
  };

  const relation = {
    relation_id: "relation-media",
    from_clock_domain_id: "media-1",
    to_clock_domain_id: "server-clock",
    offset_nano: "0",
    uncertainty_nano: "12000000",
    method: "provider_declared",
  };

  it("carries an opaque handle without inventing a digest", () => {
    const [custody] = buildMediaCustody(custodyIncident(opaque));

    expect(custody.integrity).toBe("opaque_handle");
    expect(custody.digest).toBeNull();
    expect(custody.sizeBytes).toBeNull();
    expect(custody.custodian).toBe("provider.vapi");
    expect(custody.integrityNote).toMatch(/cannot attest/);
  });

  it("attributes a declared digest to its producer, never to earshot", () => {
    const [custody] = buildMediaCustody(
      custodyIncident({
        ...opaque,
        integrity: "content_digest",
        sha256: "a".repeat(64),
        size_bytes: 4096,
        custodian: null,
      }),
    );

    expect(custody.integrity).toBe("content_digest");
    expect(custody.digest).toBe("a".repeat(64));
    // The copy must not let a reader conclude earshot checked the bytes.
    expect(custody.integrityNote).toMatch(/earshot did not read these bytes/);
    expect(custody.integrityNote).not.toMatch(/verified\b(?! it)/);
  });

  it("aligns media through a declared ClockRelation, carrying its uncertainty", () => {
    const [custody] = buildMediaCustody(custodyIncident(opaque, [relation]));

    expect(custody.alignment).toEqual({
      state: "aligned",
      note: "via relation-media",
      method: "provider_declared",
      uncertaintyMs: 12,
      driftPpm: null,
    });
  });

  it("aligns through a relation declared in the opposite direction", () => {
    const [custody] = buildMediaCustody(
      custodyIncident(opaque, [
        {
          ...relation,
          from_clock_domain_id: "server-clock",
          to_clock_domain_id: "media-1",
        },
      ]),
    );

    expect(custody.alignment.state).toBe("aligned");
  });

  it("refuses to guess an offset when no calibration reaches the session", () => {
    const [custody] = buildMediaCustody(custodyIncident(opaque));

    expect(custody.alignment.state).toBe("unaligned");
    expect(custody.alignment.note).toMatch(/no offset is assumed/);
  });

  it("does not treat a relation to an unused domain as alignment", () => {
    const [custody] = buildMediaCustody(
      custodyIncident(opaque, [{ ...relation, to_clock_domain_id: "stranded" }]),
    );

    expect(custody.alignment.state).toBe("unaligned");
  });

  it("needs no relation when the media shares the session's clock domain", () => {
    const [custody] = buildMediaCustody(
      custodyIncident({ ...opaque, clock_domain_id: "server-clock" }),
    );

    expect(custody.alignment.state).toBe("session_domain");
  });

  it("says so when a reference declares no media timeline at all", () => {
    const [custody] = buildMediaCustody(
      custodyIncident({ ...opaque, clock_domain_id: null }),
    );

    expect(custody.alignment.state).toBe("undeclared");
  });

  it("projects nothing for an incident that references no media", () => {
    expect(
      buildMediaCustody({ profile: { media_refs: [] } } as unknown as IncidentLike),
    ).toEqual([]);
  });
});
