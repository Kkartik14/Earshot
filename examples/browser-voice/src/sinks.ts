/**
 * Output-sink switching, feature-detected.
 *
 * `AudioContext.setSinkId` / the `sinkchange` event are part of the Audio Output
 * Devices API and are NOT available in every browser (notably not Firefox, and
 * historically not Safari). This module never assumes they exist: it detects
 * them, and where they are missing it simply cannot switch — the SDK then records
 * no `sink_change` event, which is the honest signal. Nothing here fabricates a
 * sink id or a change that did not happen.
 *
 * When switching IS supported, calling `setSinkId` triggers the real `sinkchange`
 * event that `recorder.attachAudioContext` is already listening for, so the SDK
 * captures the (hashed) output-route change through its normal code path.
 */

/** The structural surface of the not-everywhere AudioContext sink API. */
interface SinkSwitchableContext {
  setSinkId?: (sinkId: string) => Promise<void>;
  sinkId?: string | { type: string };
}

function asSinkSwitchable(ctx: AudioContext): SinkSwitchableContext {
  return ctx as unknown as SinkSwitchableContext;
}

/** Whether this browser's `AudioContext` can switch output devices at all. */
export function supportsSinkSelection(ctx: AudioContext): boolean {
  return typeof asSinkSwitchable(ctx).setSinkId === "function";
}

/**
 * Enumerate audio output devices for the UI dropdown. Labels are only populated
 * after microphone permission is granted, which the app requests first. These
 * labels are shown locally and never sent — the SDK hashes the sink id it
 * observes before anything leaves the client.
 */
export async function listOutputDevices(): Promise<MediaDeviceInfo[]> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((device) => device.kind === "audiooutput");
}

/**
 * Move the AudioContext's output to `deviceId`. Throws if the platform lacks the
 * API — the caller disables the control in that case rather than pretending.
 */
export async function switchSink(ctx: AudioContext, deviceId: string): Promise<void> {
  const sinkable = asSinkSwitchable(ctx);
  if (typeof sinkable.setSinkId !== "function") {
    throw new Error(
      "AudioContext.setSinkId is unavailable in this browser; output selection is " +
        "not possible here (the SDK will simply record no sink_change event).",
    );
  }
  await sinkable.setSinkId(deviceId);
}
