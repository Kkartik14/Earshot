import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ViewerQueryScopeProvider } from "../../api/queryScope";
import { fakeClocks } from "./__fixtures__/fakeMediaClock";
import exampleCall from "./__fixtures__/call-detail.json";
import exampleSpan from "./__fixtures__/llm-span.json";
import type {
  CallDetail,
  CallLiveTurns,
  CallProjection,
  CallSpan,
  CallTurn,
  PlacedLine,
} from "./api";
import { CallViewBody } from "./CallView";
import { MediaClockContext } from "./Player";
import { SpanDetail } from "./SpanDrawer";

// The call detail and span bodies are the frozen contract's examples
// (the API shapes), which are real bodies from a real server.
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

function renderCall(
  call: CallDetail,
  liveTurns?: CallTurn[],
  liveRevision?: number | null,
  liveAxisOrigin?: CallLiveTurns["axis_origin"],
  liveAxisOriginUnixNano?: string | null,
) {
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
  return {
    ...render(
      <CallViewBody
        call={call}
        liveTurns={liveTurns}
        liveRevision={liveRevision}
        liveAxisOrigin={liveAxisOrigin}
        liveAxisOriginUnixNano={liveAxisOriginUnixNano}
      />,
      { wrapper },
    ),
    clocks,
  };
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

  it("keeps turns without framework IDs in distinct rows", async () => {
    const turnsWithoutIds = baseProjection.turns.slice(0, 2).map((turn, index) => ({
      ...turn,
      index,
      turn_id: null,
    }));
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    try {
      await renderWithClock(withProjection({ turns: turnsWithoutIds }));
      expect(rows()).toHaveLength(2);
      expect(consoleError.mock.calls.flat().join(" ")).not.toMatch(/same key/i);
    } finally {
      consoleError.mockRestore();
    }
  });

  it("keeps null index keys distinct from numeric-looking framework IDs", async () => {
    const mixedTurns = baseProjection.turns.slice(0, 2).map((turn, index) => ({
      ...turn,
      index,
      turn_id: index === 0 ? null : "0",
    }));
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    try {
      await renderWithClock(withProjection({ turns: mixedTurns }));
      expect(rows()).toHaveLength(2);
      expect(consoleError.mock.calls.flat().join(" ")).not.toMatch(/same key/i);
    } finally {
      consoleError.mockRestore();
    }
  });

  it("seeks to a turn from its start", async () => {
    const { clock } = await renderWithClock(withProjection({}));
    fireEvent.click(screen.getByRole("button", { name: "Play turn 2 from 0:11.8" }));
    expect(clock.seeks).toEqual([11_810]);
  });

  it("keeps turn playback focus when a newly arrived earlier turn shifts row numbers", async () => {
    const call = withProjection({});
    const { rerender } = await renderWithClock(call);
    const playTurn = screen.getByRole("button", { name: "Play turn 2 from 0:11.8" });
    playTurn.focus();

    const earlierTurn: CallTurn = {
      ...baseProjection.turns[0],
      turn_id: "turn-earlier",
      index: 0,
      start_ms: 0,
      end_ms: 1_000,
      spans: [],
    };
    const shiftedTurns = [
      earlierTurn,
      ...baseProjection.turns.map((turn) => ({ ...turn, index: turn.index + 1 })),
    ];
    rerender(
      <CallViewBody
        call={{ ...call, projection: { ...call.projection!, turns: shiftedTurns } }}
      />,
    );

    expect(screen.getByRole("button", { name: "Play turn 3 from 0:11.8" })).toHaveFocus();
  });

  it("keeps ID-less turn focus by its parent span when earlier descendants arrive", async () => {
    const idlessTurn = {
      ...baseProjection.turns[1],
      index: 0,
      turn_id: null,
      turn_span: {
        trace_id: baseProjection.turns[1].spans[0].trace_id,
        span_id: "parent-turn",
      },
    } as CallTurn;
    const call = withProjection({ turns: [idlessTurn] });
    const { rerender } = await renderWithClock(call);
    const playTurn = screen.getByRole("button", { name: "Play turn 1 from 0:11.8" });
    playTurn.focus();

    const earlierTurn: CallTurn = {
      ...baseProjection.turns[0],
      turn_id: "turn-earlier",
      index: 0,
      start_ms: 0,
      end_ms: 1_000,
      spans: [],
    };
    const earlierChild = {
      ...idlessTurn.spans[0],
      span_id: "earlier-child",
      start_ms: 0,
    };
    const shiftedIdlessTurn = {
      ...idlessTurn,
      index: 1,
      spans: [earlierChild, ...idlessTurn.spans],
    } as CallTurn;
    rerender(
      <CallViewBody
        call={{
          ...call,
          projection: {
            ...call.projection!,
            turns: [earlierTurn, shiftedIdlessTurn],
          },
        }}
      />,
    );

    expect(screen.getByRole("button", { name: "Play turn 2 from 0:11.8" })).toHaveFocus();
  });

  it("moves focus to Turns when an open turn disappears on close without a Revision", () => {
    const openCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const { rerender } = renderCall(
      openCall,
      baseProjection.turns,
      null,
      "first_evidence",
      base.first_evidence_unix_nano,
    );
    const opener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    opener.focus();
    expect(opener).toHaveFocus();

    const closedCall: CallDetail = { ...openCall, state: "closed" };
    rerender(<CallViewBody call={closedCall} liveTurns={[]} liveRevision={null} />);

    expect(screen.getByRole("heading", { name: "Turns" })).toHaveFocus();
    expect(
      screen.getByText(
        "Turns are unavailable until a Revision captures the accepted evidence.",
      ),
    ).toBeInTheDocument();
  });

  it("restores focus when a closed Call moves turns into its current projection", async () => {
    const openCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const { rerender } = renderCall(
      openCall,
      baseProjection.turns,
      null,
      baseProjection.axis_origin,
      baseProjection.axis_origin_unix_nano,
    );
    const opener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    opener.focus();
    expect(opener).toHaveFocus();

    const currentCall = withProjection({});
    rerender(
      <CallViewBody
        call={currentCall}
        liveRevision={currentCall.projection!.revision}
        liveTurns={baseProjection.turns}
        liveAxisOrigin={baseProjection.axis_origin}
        liveAxisOriginUnixNano={baseProjection.axis_origin_unix_nano}
      />,
    );

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Turns" })).toHaveFocus(),
    );
  });

  it("keeps live turns on their own axis until matching audio and transcript are projected", () => {
    const staleCall = withProjection({ axis_origin: "recording_start" });
    staleCall.projection_state = "stale";
    staleCall.latest_revision = {
      ...staleCall.latest_revision!,
      revision: 2,
      bundle_id: "call_0d0d68c6f99a393adc2596ec43815ea9.r2",
    };
    const liveTurn: CallTurn = {
      ...baseProjection.turns[0],
      index: 0,
      start_ms: 0,
    };
    const { clocks } = renderCall(
      staleCall,
      [liveTurn],
      2,
      "first_evidence",
      (BigInt(baseProjection.axis_origin_unix_nano) - 2_000_000_000n).toString(),
    );
    expect(screen.queryByRole("button", { name: "Play" })).toBeNull();
    expect(clocks).toHaveLength(0);
    expect(
      screen.getByText(/audio and transcript from revision 1 are hidden/i),
    ).toBeInTheDocument();
    const turnsPanel = screen.getByRole("heading", { name: "Turns" }).closest("section");
    expect(turnsPanel).not.toBeNull();
    expect(within(turnsPanel as HTMLElement).getByText("0:00.0")).toBeInTheDocument();
    expect(
      within(turnsPanel as HTMLElement).queryByRole("button", { name: /play turn/i }),
    ).toBeNull();
    expect(screen.queryByRole("heading", { name: "Transcript from spans" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Transcript" })).toBeNull();
    expect(
      screen.getByText("Times are on the call's axis, from the call's first evidence."),
    ).toBeInTheDocument();
  });

  it("uses current live turns when their closed revision and axis match the projection", async () => {
    const liveTurn: CallTurn = {
      ...baseProjection.turns[0],
      index: 0,
      start_ms: 2_200,
    };
    renderCall(
      base,
      [liveTurn],
      baseProjection.revision,
      baseProjection.axis_origin,
      baseProjection.axis_origin_unix_nano,
    );

    expect(
      await screen.findByRole("button", { name: "Play turn 1 from 0:02.2" }),
    ).toBeInTheDocument();
  });

  it("announces the axis change and moves focus when playback controls disappear", async () => {
    const call = withProjection({});
    const { rerender } = await renderWithClock(call);
    const playTurn = screen.getByRole("button", { name: "Play turn 1 from 0:01.3" });
    playTurn.focus();
    expect(playTurn).toHaveFocus();

    const staleCall = withProjection({});
    staleCall.projection_state = "stale";
    staleCall.latest_revision = {
      ...staleCall.latest_revision!,
      revision: 2,
      bundle_id: "call_0d0d68c6f99a393adc2596ec43815ea9.r2",
    };
    rerender(
      <CallViewBody
        call={staleCall}
        liveRevision={2}
        liveTurns={[{ ...baseProjection.turns[0], start_ms: 0 }]}
        liveAxisOrigin="first_evidence"
        liveAxisOriginUnixNano={(
          BigInt(baseProjection.axis_origin_unix_nano) - 2_000_000_000n
        ).toString()}
      />,
    );

    const notice = screen.getByText(/audio and transcript from revision 1 are hidden/i);
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveAttribute("aria-live", "polite");
    await waitFor(() => expect(notice).toHaveFocus());

    const alignedCall = withProjection({});
    rerender(
      <CallViewBody
        call={alignedCall}
        liveRevision={alignedCall.projection!.revision}
        liveTurns={baseProjection.turns}
        liveAxisOrigin={baseProjection.axis_origin}
        liveAxisOriginUnixNano={baseProjection.axis_origin_unix_nano}
      />,
    );
    await screen.findByRole("button", { name: "Play" });
    const restoredNotice = screen.getByText(
      "Audio and transcript now match the current turn axis.",
    );
    expect(restoredNotice).toHaveAttribute("role", "status");
    await waitFor(() => expect(restoredNotice).toHaveFocus());
  });

  it("restores focus when an open-call turn detail becomes unaligned on close", async () => {
    const openCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const { rerender } = renderCall(
      openCall,
      baseProjection.turns,
      null,
      "first_evidence",
      base.first_evidence_unix_nano,
    );
    const opener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    opener.focus();
    expect(opener).toHaveFocus();

    const staleCall = withProjection({});
    staleCall.projection_state = "stale";
    staleCall.latest_revision = {
      ...staleCall.latest_revision!,
      revision: 2,
      bundle_id: "call_0d0d68c6f99a393adc2596ec43815ea9.r2",
    };
    rerender(
      <CallViewBody
        call={staleCall}
        liveRevision={2}
        liveTurns={baseProjection.turns}
        liveAxisOrigin="first_evidence"
        liveAxisOriginUnixNano={(
          BigInt(base.first_evidence_unix_nano) - 2_000_000_000n
        ).toString()}
      />,
    );

    const notice = screen.getByText(/Live turns use the latest Call axis/);
    await waitFor(() => expect(notice).toHaveFocus());
  });

  it("announces an exact live-axis rebase when the turn count stays the same", () => {
    const openCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const firstOrigin = BigInt(base.first_evidence_unix_nano);
    const turn = { ...baseProjection.turns[0], start_ms: 0 };
    const { rerender } = renderCall(
      openCall,
      [turn],
      null,
      "first_evidence",
      firstOrigin.toString(),
    );
    expect(screen.queryByText(/live time axis changed/i)).toBeNull();

    rerender(
      <CallViewBody
        call={openCall}
        liveTurns={[{ ...turn, start_ms: 2_000 }]}
        liveAxisOrigin="first_evidence"
        liveAxisOriginUnixNano={(firstOrigin - 2_000_000_000n).toString()}
      />,
    );

    expect(
      screen.getByText("The live time axis changed once. Turn times were updated."),
    ).toHaveAttribute("role", "status");
  });

  it("places an unprojected span transcript on the live turns axis", () => {
    const transcriptStart = BigInt(base.first_evidence_unix_nano) + 20_000_000_000n;
    const liveOrigin = BigInt(base.first_evidence_unix_nano) + 10_000_000_000n;
    const liveCall: CallDetail = {
      ...base,
      state: "open",
      projection_state: "none",
      projection: null,
      transcript: [
        {
          ...base.transcript[0],
          start_unix_nano: transcriptStart.toString(),
        },
      ],
    };

    renderCall(
      liveCall,
      baseProjection.turns,
      null,
      "first_evidence",
      liveOrigin.toString(),
    );

    const transcriptLine = screen.getByText(base.transcript[0].text).closest("li");
    expect(transcriptLine).toHaveTextContent("0:10.0");
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

  it("restores focus after live rows shift and projection replaces their table", async () => {
    const liveCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const { rerender } = renderCall(liveCall, baseProjection.turns);
    const liveOpener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    fireEvent.click(liveOpener);
    const dialog = screen.getByRole("dialog", { name: "llm span detail" });

    const earlierTurn: CallTurn = {
      ...baseProjection.turns[0],
      turn_id: "turn-earlier",
      index: 0,
      start_ms: 0,
      spans: [],
    };
    const turnsWithEarlierParent = [
      earlierTurn,
      ...baseProjection.turns.map((turn) => ({ ...turn, index: turn.index + 1 })),
    ];
    rerender(<CallViewBody call={liveCall} liveTurns={turnsWithEarlierParent} />);
    await act(async () => {});
    rerender(<CallViewBody call={withProjection({})} liveTurns={baseProjection.turns} />);
    await act(async () => {});
    expect(liveOpener.isConnected).toBe(false);
    const projectedOpener = within(rows()[0]).getByRole("button", { name: "LLM llm" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await act(async () => {});

    expect(projectedOpener).toHaveFocus();
  });

  it("announces new turns in a polite live region", async () => {
    const liveCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const { rerender } = renderCall(liveCall, baseProjection.turns);
    const turnsPanel = screen.getByRole("heading", { name: "Turns" }).closest("section");
    expect(turnsPanel).not.toBeNull();
    const announcement = within(turnsPanel as HTMLElement).getByRole("status");
    expect(announcement).toHaveAttribute("aria-live", "polite");
    expect(announcement).toBeEmptyDOMElement();

    const newTurn: CallTurn = {
      ...baseProjection.turns[0],
      turn_id: "turn-new",
      index: baseProjection.turns.length,
    };
    rerender(
      <CallViewBody call={liveCall} liveTurns={[...baseProjection.turns, newTurn]} />,
    );

    expect(
      await screen.findByText(
        `1 new turn received. ${baseProjection.turns.length + 1} turns total.`,
      ),
    ).toHaveAttribute("aria-live", "polite");
  });

  it("resets the live-turn announcement when navigating to another Call", async () => {
    const firstCall: CallDetail = {
      ...base,
      state: "open",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    const secondCall: CallDetail = {
      ...firstCall,
      call_id: "call_abcdef0123456789abcdef0123456789",
    };
    const firstTurns = baseProjection.turns.slice(0, 2);
    const secondTurns = [
      ...baseProjection.turns,
      {
        ...baseProjection.turns[0],
        turn_id: "turn-new-a",
        index: baseProjection.turns.length,
      },
    ];
    const { rerender } = renderCall(firstCall, firstTurns);
    const turnAnnouncement = () => {
      const turnsPanel = screen
        .getByRole("heading", { name: "Turns" })
        .closest("section");
      return within(turnsPanel as HTMLElement).getByRole("status");
    };
    expect(turnAnnouncement()).toBeEmptyDOMElement();

    rerender(<CallViewBody call={secondCall} liveTurns={secondTurns} />);
    await act(async () => {});

    expect(turnAnnouncement()).toBeEmptyDOMElement();
  });

  it("does not claim that no turns were captured before a closed Call gets a Revision", () => {
    const closedWithoutRevision: CallDetail = {
      ...base,
      state: "closed",
      latest_revision: null,
      projection_state: "none",
      projection: null,
    };
    renderCall(closedWithoutRevision, []);

    expect(
      screen.getByText(
        "Turns are unavailable until a Revision captures the accepted evidence.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No parent turn spans were captured for this call."),
    ).toBeNull();
    expect(screen.queryByText("No turns were received for this call.")).toBeNull();
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

describe("a call without a projection (the span-transcript view)", () => {
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

  it("does not invent a timestamp for an order-only span transcript line", () => {
    renderCall({
      ...base,
      projection: null,
      projection_state: "none",
      transcript: base.transcript.map((line) => ({
        ...line,
        start_unix_nano: null,
        end_unix_nano: null,
        timing: "order_only",
      })),
    });

    expect(screen.getByText("—")).toBeInTheDocument();
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
