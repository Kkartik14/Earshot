import {
  LANE_HEIGHT,
  type LaneSpeaker,
  type MediaClock,
  type PlayerSource,
} from "./mediaClock";

const LANE_TOKENS: Record<LaneSpeaker, [token: string, fallback: string]> = {
  user: ["--stt", "#0c90ac"],
  agent: ["--agent", "#4b57c4"],
  mixed: ["--tx-mid", "#54607a"],
};

/** `#rrggbb` at `alpha`; any other color as given. */
function translucent(color: string, alpha: number): string {
  return /^#[0-9a-f]{6}$/i.test(color)
    ? `${color}${Math.round(alpha * 255)
        .toString(16)
        .padStart(2, "0")}`
    : color;
}

/** The production clock: wavesurfer.js over one HTML media element.
 *
 * The media element streams `source.url` (the browser sends Range requests), and
 * the waveform is drawn from the server's peaks, so the audio is never fetched
 * whole or decoded in the page. One lane per channel (`splitChannels`). Without
 * peaks yet, the lanes are flat and the duration comes from the media metadata.
 * wavesurfer is loaded on first use, so views without a player never load it. */
export async function createWavesurferClock(source: PlayerSource): Promise<MediaClock> {
  const { default: WaveSurfer } = await import("wavesurfer.js");
  const style = getComputedStyle(source.container);
  const token = ([name, fallback]: [string, string]) =>
    style.getPropertyValue(name).trim() || fallback;

  const wavesurfer = WaveSurfer.create({
    container: source.container,
    url: source.url,
    peaks: source.peaks ?? source.lanes.map(() => new Float32Array(2)),
    duration: source.durationMs == null ? undefined : source.durationMs / 1000,
    height: LANE_HEIGHT,
    splitChannels: source.lanes.map((speaker) => {
      const color = token(LANE_TOKENS[speaker]);
      return { waveColor: translucent(color, 0.45), progressColor: color };
    }),
    cursorColor: token(["--tx", "#0f1626"]),
    cursorWidth: 1,
    normalize: false,
    dragToSeek: true,
    hideScrollbar: true,
  });

  const toAxis = (seconds: number) => source.offsetMs + seconds * 1000;
  const timeListeners = new Set<(ms: number) => void>();
  const playingListeners = new Set<(playing: boolean) => void>();
  const report = (ms: number) => timeListeners.forEach((listener) => listener(ms));
  const playing = (value: boolean) =>
    playingListeners.forEach((listener) => listener(value));
  const unsubscribe = [
    wavesurfer.on("timeupdate", (seconds) => report(toAxis(seconds))),
    wavesurfer.on("play", () => playing(true)),
    wavesurfer.on("pause", () => playing(false)),
    wavesurfer.on("finish", () => playing(false)),
    wavesurfer.on("error", (error) => source.onError(error.message)),
  ];

  return {
    play() {
      wavesurfer.play().catch((error: unknown) => {
        // A pause() or a new load supersedes a pending play(): not a failure.
        if (error instanceof DOMException && error.name === "AbortError") return;
        source.onError(error instanceof Error ? error.message : String(error));
      });
    },
    pause() {
      wavesurfer.pause();
    },
    seek(ms) {
      const duration = wavesurfer.getDuration();
      const seconds = Math.max(0, (ms - source.offsetMs) / 1000);
      wavesurfer.setTime(duration > 0 ? Math.min(seconds, duration) : seconds);
      report(toAxis(wavesurfer.getCurrentTime()));
    },
    get currentTime() {
      return toAxis(wavesurfer.getCurrentTime());
    },
    onTime(listener) {
      timeListeners.add(listener);
      return () => timeListeners.delete(listener);
    },
    onPlaying(listener) {
      playingListeners.add(listener);
      return () => playingListeners.delete(listener);
    },
    destroy() {
      unsubscribe.forEach((stop) => stop());
      timeListeners.clear();
      playingListeners.clear();
      wavesurfer.destroy();
    },
  };
}
