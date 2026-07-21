import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import {
  buildSummary,
  buildTimeline,
  buildTurnDetails,
  getCoverage,
  type AnalysisLike,
  type IncidentLike,
} from "./timeline";

const incident = incidentFixture as unknown as IncidentLike;
const analysis = analysisFixture as unknown as AnalysisLike;

describe("buildTimeline", () => {
  const timeline = buildTimeline(incident, analysis);

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

  it("lays stages out turn-relative from the session clock", () => {
    const [stt, llm, tts] = timeline.turns[0].stages;
    expect(stt.startMs).toBe(0);
    expect(llm.startMs).toBe(300);
    expect(tts.startMs).toBe(770);
    expect(llm.endMs).toBe(tts.startMs);
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
  const summary = buildSummary(incident, buildTimeline(incident, analysis));

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
});

describe("buildTurnDetails", () => {
  const details = buildTurnDetails(incident, analysis);

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
});

describe("getCoverage", () => {
  it("reports only the signals that were not fully observed", () => {
    const gaps = getCoverage(incident);
    expect(gaps.some((g) => g.signal === "client.render")).toBe(true);
    expect(gaps.every((g) => g.availability !== "available")).toBe(true);
    expect(gaps.find((g) => g.signal === "client.render")?.reason).toContain("client");
  });
});
