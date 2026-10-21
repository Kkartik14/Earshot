import type { CallMedia, CallProjection } from "./api";

const SIGNALS_EXTRA_REQUIRED = "EARSHOT_SIGNALS_EXTRA_REQUIRED";
const INSTALL_SIGNALS =
  "install the server's signals extra (pip install 'earshot-observability[signals]') and restart it";

/** Why one recording has no waveform yet, or null once its analysis is stored. */
export function mediaAnalysisNote(media: CallMedia): string | null {
  const code = media.analysis_error_code;
  switch (media.analysis_state) {
    case "stored":
      return null;
    case "pending":
    case "running":
      return "The audio analysis is still running: the waveform and the audible gaps appear when it finishes.";
    case "failed":
      return `The audio analysis failed${code ? ` (${code})` : ""}: there is no waveform for this recording.`;
    case "unavailable":
      return code === SIGNALS_EXTRA_REQUIRED
        ? `The audio analysis is unavailable: ${INSTALL_SIGNALS}.`
        : `The audio analysis is unavailable${code ? ` (${code})` : ""}.`;
  }
}

/** What the call's analysis state means for its audible gaps and signals. */
export function callAnalysisNote(projection: CallProjection): string | null {
  const needsExtra = projection.media.some(
    (media) => media.analysis_error_code === SIGNALS_EXTRA_REQUIRED,
  );
  switch (projection.analysis_state) {
    case "stored":
      return null;
    case "none":
      return "This call has no recording: there is no audio, and audible gaps are not measurable.";
    case "pending":
      return "Analysis pending: audible gaps and signals appear when the audio analysis finishes.";
    case "partial":
      return "Some of this call's recordings could not be analysed: audible gaps and signals that need them are missing.";
    case "failed":
      return "The audio analysis failed: audible gaps and signals are not measurable.";
    case "unavailable":
      return needsExtra
        ? `Analysis unavailable: ${INSTALL_SIGNALS}.`
        : "Analysis unavailable: audible gaps and signals are not measurable.";
  }
}
