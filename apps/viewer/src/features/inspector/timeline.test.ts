import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import {
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  getCoverage,
  type AnalysisLike,
  type ExplanationLike,
  type IncidentLike,
} from "./timeline";

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
