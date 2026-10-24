/** Shared playback clock for transcript highlighting and seeking; production and tests use different adapters. */
export interface MediaClock {
  play(): void;
  pause(): void;
  /** Move the playhead to `ms` on the call axis (clamped to the recording). The
   * new time is reported to `onTime` listeners at once, paused or not. */
  seek(ms: number): void;
  /** The playhead on the call axis. */
  readonly currentTime: number;
  /** Called with the playhead whenever it moves; returns the unsubscribe. */
  onTime(listener: (ms: number) => void): () => void;
  /** Called when playback starts or stops; returns the unsubscribe. */
  onPlaying(listener: (playing: boolean) => void): () => void;
  destroy(): void;
}

export type LaneSpeaker = "user" | "agent" | "mixed";

/** What a clock plays and draws: one media entry of the call's projection. */
export interface PlayerSource {
  /** The element the waveform lanes are drawn into. */
  container: HTMLElement;
  /** The same-origin content URL; the browser streams it with Range requests. */
  url: string;
  /** Per channel, the parsed peaks (-1..1); null while the server has none. */
  peaks: Float32Array[] | null;
  durationMs: number | null;
  /** Where the file's first sample sits on the call axis. */
  offsetMs: number;
  /** One lane per file channel, in channel order. */
  lanes: LaneSpeaker[];
  onError(message: string): void;
}

export type MediaClockFactory = (source: PlayerSource) => Promise<MediaClock>;

/** One waveform lane's height in pixels: the lane labels line up with it. */
export const LANE_HEIGHT = 56;
