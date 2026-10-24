import { act } from "@testing-library/react";
import type { MediaClock, MediaClockFactory, PlayerSource } from "../mediaClock";

/** A declared fake (AGENT_RULES rule 4): the call views' `MediaClock`.
 *
 * jsdom has no media playback and no canvas, so the wavesurfer adapter cannot run
 * in a unit test. This fake stands in for it at the one seam the call views
 * declare. It records what it was asked to play (the source: URL, peaks, lanes)
 * and every seek, and a test moves its playhead as playback would. Everything
 * else in the view (data, rendering, highlight and seek wiring) is real. */
export class FakeMediaClock implements MediaClock {
  currentTime = 0;
  playing = false;
  destroyed = false;
  readonly seeks: number[] = [];
  private readonly timeListeners = new Set<(ms: number) => void>();
  private readonly playingListeners = new Set<(playing: boolean) => void>();

  constructor(readonly source: PlayerSource) {}

  play(): void {
    this.setPlaying(true);
  }

  pause(): void {
    this.setPlaying(false);
  }

  seek(ms: number): void {
    this.seeks.push(ms);
    this.currentTime = ms;
    this.timeListeners.forEach((listener) => listener(ms));
  }

  onTime(listener: (ms: number) => void): () => void {
    this.timeListeners.add(listener);
    return () => this.timeListeners.delete(listener);
  }

  onPlaying(listener: (playing: boolean) => void): () => void {
    this.playingListeners.add(listener);
    return () => this.playingListeners.delete(listener);
  }

  destroy(): void {
    this.destroyed = true;
  }

  /** Playback reaches `ms` on the call axis. */
  playTo(ms: number): void {
    act(() => {
      this.currentTime = ms;
      this.timeListeners.forEach((listener) => listener(ms));
    });
  }

  private setPlaying(value: boolean): void {
    this.playing = value;
    this.playingListeners.forEach((listener) => listener(value));
  }
}

/** A factory for `MediaClockContext` that keeps every clock it made. */
export function fakeClocks(): { factory: MediaClockFactory; clocks: FakeMediaClock[] } {
  const clocks: FakeMediaClock[] = [];
  return {
    clocks,
    factory: async (source) => {
      const clock = new FakeMediaClock(source);
      clocks.push(clock);
      return clock;
    },
  };
}
