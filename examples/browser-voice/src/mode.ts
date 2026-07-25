/** Shared shape returned by a voice mode (loopback or OpenAI Realtime). */
export interface VoiceModeHandle {
  /** The live render context, exposed so the UI can drive sink switching. */
  audioCtx: AudioContext;
  /** Whether this browser can switch the AudioContext output device. */
  supportsSinkSwitch: boolean;
  /** Set the monitor volume of the received audio (0 = silent, 1 = full). */
  setMonitorGain(value: number): void;
  /** Tear down the mode-specific media resources (peer connection, context). */
  stop(): Promise<void>;
}
