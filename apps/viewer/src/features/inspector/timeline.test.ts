import { describe, expect, it } from "vitest";
import analysisFixture from "./__fixtures__/analysis.json";
import incidentFixture from "./__fixtures__/incident.json";
import { buildTimeline, type AnalysisLike, type IncidentLike } from "./timeline";

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
