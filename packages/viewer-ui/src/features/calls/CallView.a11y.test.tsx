import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { ViewerQueryScopeProvider } from "../../api/queryScope";
import { fakeClocks } from "./__fixtures__/fakeMediaClock";
import exampleCall from "./__fixtures__/call-detail.json";
import exampleSpan from "./__fixtures__/llm-span.json";
import type { CallDetail, CallProjection, CallSpan, CallTurn, PlacedLine } from "./api";
import { CallViewBody } from "./CallView";
import { MediaClockContext } from "./Player";
import { SpanDetail } from "./SpanDrawer";

// The call detail and span bodies are the frozen contract's examples
// (`month-2/api-shapes.md` §4 and §5), which are real bodies from a real server.
// The views get them as data; only the media clock is the declared fake.
const base = exampleCall as CallDetail;
const baseProjection = base.projection as CallProjection;

function withProjection(changes: Partial<CallProjection>): CallDetail {
  return {
    ...base,
    projection: {
      ...baseProjection,
      // No peaks request: the waveform is the clock's business, not this test's.
      media: baseProjection.media.map((media) => ({ ...media, peaks_path: null })),
      ...changes,
    },
  };
}

function renderCall(call: CallDetail) {
  const { factory, clocks } = fakeClocks();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ViewerQueryScopeProvider scope={{ projectId: "default", authContextId: "test" }}>
        <MediaClockContext.Provider value={factory}>
          {children}
        </MediaClockContext.Provider>
      </ViewerQueryScopeProvider>
    </QueryClientProvider>
  );
  return { ...render(<CallViewBody call={call} />, { wrapper }), clocks };
}

async function renderWithClock(call: CallDetail) {
  const rendered = renderCall(call);
  await screen.findByRole("button", { name: "Play" });
  await act(async () => {});
  expect(rendered.clocks).toHaveLength(1);
  return { ...rendered, clock: rendered.clocks[0] };
}

const transcript = () => within(screen.getByRole("region", { name: "Transcript" }));
const currentLine = () =>
  transcript()
    .getAllByRole("listitem")
    .filter((item) => item.getAttribute("aria-current") === "time")
    .map((item) => item.textContent);

describe("call view player", () => {
  it("plays the projection's content path with one labelled lane per channel", async () => {
    const { clock } = await renderWithClock(withProjection({}));

    expect(clock.source).toMatchObject({
      url: "/v1/recordings/conv-1/content",
      lanes: ["user", "agent"],
      offsetMs: 0,
      durationMs: 32486,
      peaks: null,
    });
    const lanes = within(screen.getByRole("list", { name: "Lanes" }));
    expect(lanes.getAllByRole("listitem").map((lane) => lane.textContent)).toEqual([
      "User",
      "Agent",
    ]);
  });

  it("offers play, pause and skips as buttons wired to the clock", async () => {
    const { clock } = await renderWithClock(withProjection({}));

    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(clock.playing).toBe(true);
    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    clock.playTo(10_000);
    fireEvent.click(screen.getByRole("button", { name: "Back 5 seconds" }));
    expect(clock.seeks).toEqual([5_000]);
    expect(screen.getByLabelText("Playhead")).toHaveTextContent("0:05.0");
  });

  it("says why there is no waveform while the analysis runs", async () => {
    await renderWithClock(
      withProjection({
        media: baseProjection.media.map((media) => ({
          ...media,
          peaks_path: null,
          analysis_state: "pending",
        })),
      }),
    );
    expect(screen.getByText(/analysis is still running/i)).toBeInTheDocument();
  });
});

describe("synced transcript", () => {
  it("highlights the line under the playhead, and none between lines", async () => {
    const { clock } = await renderWithClock(withProjection({}));

    clock.playTo(2_000);
    expect(currentLine()).toEqual([expect.stringContaining("book a table")]);
    clock.playTo(5_000);
    expect(currentLine()).toEqual([]);
    clock.playTo(7_000);
    expect(currentLine()).toEqual([expect.stringContaining("what time")]);
  });

  it("seeks the player when a timed line is activated", async () => {
    const { clock } = await renderWithClock(withProjection({}));

    const line = transcript().getByRole("button", { name: /what time would you like/ });
    line.focus();
    expect(line).toHaveFocus();
    fireEvent.click(line);

    expect(clock.seeks).toEqual([6_960]);
    expect(currentLine()).toEqual([expect.stringContaining("what time")]);
  });

  it("lists an order-only line in order but never seeks to it", async () => {
    const unplaced: PlacedLine = {
      line_no: 2,
      speaker: "user",
      text: "Seven thirty, please.",
      timing: "order_only",
      start_ms: null,
      end_ms: null,
      alignment_uncertain: false,
      span: null,
    };
    const { clock } = await renderWithClock(
      withProjection({ transcript: [...baseProjection.transcript, unplaced] }),
    );

    const item = transcript().getByText("Seven thirty, please.").closest("li");
    expect(item).not.toBeNull();
    expect(within(item as HTMLElement).queryByRole("button")).toBeNull();
    expect(item).toHaveTextContent("not placed on the audio");
    clock.playTo(40_000);
    expect(currentLine()).toEqual([]);
  });

  it("keeps a start-only line current until a later line starts", async () => {
    const startOnly: PlacedLine = {
      ...baseProjection.transcript[1],
      timing: "start_only",
      end_ms: null,
    };
    const { clock } = await renderWithClock(
      withProjection({ transcript: [baseProjection.transcript[0], startOnly] }),
    );

    clock.playTo(30_000);
    expect(currentLine()).toEqual([expect.stringContaining("what time")]);
  });

  it("flags a line whose turn had a timeline discontinuity", async () => {
    await renderWithClock(withProjection({}));
    const uncertain = transcript().getByRole("button", { name: /what time/ });
    expect(uncertain).toHaveTextContent("approximate placement");
  });
});

describe("turns table", () => {
  const talkOver: CallTurn = {
    index: 2,
    turn_id: "turn-3",
    start_ms: 22_590,
    end_ms: 28_770,
    reported_latency_ms: null,
    audible_gap: {
      status: "talk_over",
      value_ms: null,
      uncertainty_ms: null,
      bound_ms: null,
      user_end_ms: null,
      agent_start_ms: null,
      reason: null,
      basis: null,
    },
    spans: [],
  };

  const rows = () =>
    within(screen.getByRole("table", { name: /reported latency/i }))
      .getAllByRole("row")
      .slice(1);

  it("shows the reported latency beside the audible gap at the agent", async () => {
    await renderWithClock(withProjection({ turns: [...baseProjection.turns, talkOver] }));
    const [first, second, third] = rows();

    // Turn 1: behind a recorder discontinuity, only an upper bound, with why.
    expect(within(first).getByRole("rowheader")).toHaveTextContent("1");
    expect(first).toHaveTextContent("1.80 s");
    expect(first).toHaveTextContent("≤ 3.08 s");
    expect(first).toHaveTextContent(/timeline jumps/);
    // Turn 2: measured, with its uncertainty and basis.
    expect(second).toHaveTextContent("1.35 s");
    expect(second).toHaveTextContent("1.51 s ± 0.03");
    expect(second).toHaveTextContent("recorder timeline checked");
    // Turn 3: no number at all, and never a zero.
    expect(third).toHaveTextContent("talk-over");
    expect(third).toHaveTextContent("not reported");
    expect(third).not.toHaveTextContent(/0\.00 s/);
  });

  it("seeks to a turn from its start", async () => {
    const { clock } = await renderWithClock(withProjection({}));
    fireEvent.click(screen.getByRole("button", { name: "Play turn 2 from 0:11.8" }));
    expect(clock.seeks).toEqual([11_810]);
  });

  it("opens a span's details as a dialog, closes on Escape and restores focus", async () => {
    await renderWithClock(withProjection({}));
    const opener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    expect(opener).toHaveAttribute("aria-haspopup", "dialog");

    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "llm span detail" });
    expect(opener).toHaveAttribute("aria-expanded", "true");
    expect(within(dialog).getByRole("button", { name: "Close detail" })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
});

describe("signals and cost", () => {
  it("shows the cost lower bound with its unknown items and the estimate apart", async () => {
    await renderWithClock(withProjection({}));
    const cost = within(screen.getByRole("region", { name: "Cost" }));

    expect(cost.getByText("≥ $0.00367 · 2 unknown")).toBeInTheDocument();
    expect(cost.getByText("+ ~$0.00417 (estimated)")).toBeInTheDocument();
    const statuses = cost
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.lastElementChild?.textContent);
    expect(statuses).toEqual(["priced", "not reported", "estimated"]);
    expect(cost.getByText("$0.0077 per 60 audio second")).toBeInTheDocument();
  });

  it("never shows a call with no cost item as $0", async () => {
    await renderWithClock(
      withProjection({
        cost: {
          lower_bound_nano_usd: null,
          estimated_nano_usd: null,
          unknown_items: 0,
          complete: false,
        },
        cost_items: [],
      }),
    );
    const cost = within(screen.getByRole("region", { name: "Cost" }));
    expect(cost.queryByText(/\$0\b/)).toBeNull();
    expect(cost.getByText(/no cost items/i)).toBeInTheDocument();
  });

  it("leaves cross-channel signals unknown when the tracks are not on one timeline", async () => {
    await renderWithClock(
      withProjection({
        signals: {
          one_timeline: false,
          talk_time_user_ms: 8210,
          talk_time_agent_ms: 8130,
          overlap_ms: null,
          barge_in_count: null,
          talk_over_count: null,
          long_silence_count: null,
          dead_air_ms: null,
        },
      }),
    );
    const signals = within(screen.getByRole("region", { name: "Signals" }));

    expect(signals.getByText(/not on one timeline/)).toBeInTheDocument();
    expect(signals.getByText("User talk time").nextSibling).toHaveTextContent("8.21 s");
    expect(signals.getByText("Barge-ins").nextSibling).toHaveTextContent("—");
    expect(signals.getByText("Dead air").nextSibling).toHaveTextContent("—");
  });

  it("names the signals extra when the analysis is unavailable without it", async () => {
    await renderWithClock(
      withProjection({
        analysis_state: "unavailable",
        media: baseProjection.media.map((media) => ({
          ...media,
          peaks_path: null,
          analysis_state: "unavailable",
          analysis_error_code: "EARSHOT_SIGNALS_EXTRA_REQUIRED",
        })),
      }),
    );
    expect(screen.getAllByText(/signals extra/).length).toBeGreaterThan(0);
  });
});

describe("a call without a projection (the Month 1 view)", () => {
  it("shows the transcript read from its spans and no turns, analysis or cost", () => {
    renderCall({ ...base, projection: null, projection_state: "none" });

    expect(
      screen.getByRole("heading", { name: "Transcript from spans" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/book a table for two people/)).toBeInTheDocument();
    expect(screen.getByText(/not projected yet/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Turns" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Cost" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Play" })).toBeNull();
  });

  it("links the call to its revision's incident", () => {
    renderCall({ ...base, projection: null, projection_state: "none" });
    expect(screen.getByRole("link", { name: "revision 1 incident" })).toHaveAttribute(
      "href",
      "/sessions/call_0d0d68c6f99a393adc2596ec43815ea9.r1",
    );
  });
});

describe("span details", () => {
  const span = exampleSpan as CallSpan;

  it("shows an LLM span's input and output, then its metadata", () => {
    render(<SpanDetail span={span} />);

    expect(
      screen.getByRole("heading", { name: "LLM input" }).nextSibling,
    ).toHaveTextContent("Hi. I would like to book a tab");
    expect(
      screen.getByRole("heading", { name: "LLM output" }).nextSibling,
    ).toHaveTextContent("Sure, what time would you like to come in tomorrow evening?");
    expect(screen.getByText("gen_ai.usage.input_tokens").nextSibling).toHaveTextContent(
      "119",
    );
  });

  it("says when the preset kept no content", () => {
    render(
      <SpanDetail
        span={{
          ...span,
          attributes: span.attributes.filter(
            (attribute) => attribute.capture_class === "metadata",
          ),
        }}
      />,
    );
    expect(screen.getByText(/no content is stored for this span/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "LLM input" })).toBeNull();
  });

  it("shows an error status with its exception events", () => {
    render(
      <SpanDetail
        span={{
          ...span,
          status: { code: "error", message: "upstream timed out" },
          events: [
            {
              name: "exception",
              time_unix_nano: span.end_unix_nano,
              attributes: [
                {
                  key: "exception.type",
                  semantic_key: "exception.type",
                  capture_class: "diagnostic_payload",
                  value: "TimeoutError",
                },
              ],
            },
          ],
        }}
      />,
    );
    const error = screen.getByRole("heading", { name: "Error" })
      .parentElement as HTMLElement;
    expect(error).toHaveTextContent("upstream timed out");
    expect(error).toHaveTextContent("exception.type");
    expect(error).toHaveTextContent("TimeoutError");
  });
});
