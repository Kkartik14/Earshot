import { createContext, useContext, useEffect, useRef, useState } from "react";
import { mediaUrl, usePeaks, type CallMedia } from "./api";
import { mediaAnalysisNote } from "./analysis";
import { formatClock } from "./format";
import {
  LANE_HEIGHT,
  type LaneSpeaker,
  type MediaClock,
  type MediaClockFactory,
} from "./mediaClock";
import { createWavesurferClock } from "./wavesurferClock";
import styles from "./CallView.module.css";

/** Which clock the players create: wavesurfer in the app, a declared fake in tests. */
export const MediaClockContext = createContext<MediaClockFactory>(createWavesurferClock);

const SKIP_MS = 5_000;
const LANE_LABEL = { user: "User", agent: "Agent", mixed: "Mixed" } as const;

/** One playable source: transport controls, a labelled lane per channel, and the
 * waveform drawn from the server's peaks. The audio streams from `content_path`. */
export function Player({
  media,
  label,
  onClock,
}: {
  media: CallMedia;
  label: string;
  /** Receives this player's clock once it exists, and null when it goes away. */
  onClock?: (clock: MediaClock | null) => void;
}) {
  const createClock = useContext(MediaClockContext);
  const peaks = usePeaks(media.peaks_path);
  const container = useRef<HTMLDivElement>(null);
  const [clock, setClock] = useState<MediaClock | null>(null);
  const [playing, setPlaying] = useState(false);
  const [tenths, setTenths] = useState(Math.floor(media.offset_ms / 100));
  const [error, setError] = useState<string | null>(null);
  const reportClock = useRef(onClock);
  useEffect(() => {
    reportClock.current = onClock;
  });

  const url = mediaUrl(media.content_path);
  // Wait for the peaks request to settle, so the clock is made once, with them.
  const peaksSettled = media.peaks_path == null || !peaks.isPending;
  // Peaks drawn on the wrong lanes would mislabel who spoke: use them only when
  // they have one channel per lane, as the server promises.
  const peaksMatch = peaks.data?.channels.length === media.channels.length;
  const peaksData = peaksMatch ? (peaks.data?.channels ?? null) : null;
  const lanesKey = media.channels.map((channel) => channel.speaker).join(",");

  useEffect(() => {
    const element = container.current;
    if (element == null || url == null || !peaksSettled) return;
    let made: MediaClock | null = null;
    let cancelled = false;
    const stops: (() => void)[] = [];
    setError(null);
    createClock({
      container: element,
      url,
      peaks: peaksData,
      durationMs: media.duration_ms,
      offsetMs: media.offset_ms,
      lanes: lanesKey.split(",") as LaneSpeaker[],
      onError: (message) => setError(message),
    }).then(
      (next) => {
        if (cancelled) {
          next.destroy();
          return;
        }
        made = next;
        stops.push(
          next.onPlaying(setPlaying),
          next.onTime((ms) => setTenths(Math.floor(ms / 100))),
        );
        setClock(next);
        reportClock.current?.(next);
      },
      (cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      },
    );
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
      if (made != null) {
        made.destroy();
        reportClock.current?.(null);
      }
      setClock(null);
      setPlaying(false);
    };
  }, [
    createClock,
    url,
    peaksSettled,
    peaksData,
    media.duration_ms,
    media.offset_ms,
    lanesKey,
  ]);

  const analysisNote = mediaAnalysisNote(media);
  const peaksNote =
    analysisNote ??
    (peaks.isError
      ? `The waveform could not be loaded (${peaks.error.message}).`
      : peaks.data != null && !peaksMatch
        ? `The waveform has ${peaks.data.channels.length} channels for ${media.channels.length} lanes, so it is not drawn.`
        : null);
  const end =
    media.duration_ms == null ? null : formatClock(media.offset_ms + media.duration_ms);

  return (
    <section className={styles.player} aria-label={label}>
      <div className={styles.transport}>
        <button
          type="button"
          className={styles.play}
          disabled={clock == null}
          onClick={() => (playing ? clock?.pause() : clock?.play())}
        >
          {playing ? "Pause" : "Play"}
        </button>
        <button
          type="button"
          className={styles.skip}
          disabled={clock == null}
          aria-label="Back 5 seconds"
          onClick={() => clock?.seek(clock.currentTime - SKIP_MS)}
        >
          −5 s
        </button>
        <button
          type="button"
          className={styles.skip}
          disabled={clock == null}
          aria-label="Forward 5 seconds"
          onClick={() => clock?.seek(clock.currentTime + SKIP_MS)}
        >
          +5 s
        </button>
        <span className={styles.clock}>
          <span aria-label="Playhead">{formatClock(tenths * 100)}</span>
          {end == null ? null : <span className={styles.dim}> / {end}</span>}
        </span>
        <span className={styles.source}>
          {media.source === "rendition"
            ? `stereo rendition of ${media.recording_ids.length} mono recordings`
            : media.content_type}
        </span>
      </div>

      <div className={styles.lanes}>
        <ol className={styles.laneLabels} aria-label="Lanes">
          {media.channels.map((channel) => (
            <li
              key={channel.index}
              className={styles.laneLabel}
              data-speaker={channel.speaker}
              style={{ height: LANE_HEIGHT }}
            >
              {LANE_LABEL[channel.speaker]}
            </li>
          ))}
        </ol>
        {/* The waveform is a pointer target only; the transport, transcript and
            turns give keyboard users every seek it offers. */}
        <div
          ref={container}
          className={styles.wave}
          aria-hidden="true"
          style={{ minHeight: LANE_HEIGHT * Math.max(1, media.channels.length) }}
        />
      </div>

      {url == null ? (
        <p className={styles.alert} role="alert">
          This recording has no playable path.
        </p>
      ) : null}
      {error ? (
        <p className={styles.alert} role="alert">
          The audio could not be played: {error}
        </p>
      ) : null}
      {peaksNote ? <p className={styles.note}>{peaksNote}</p> : null}
    </section>
  );
}
