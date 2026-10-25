/** Feature-detect output-device switching; unsupported browsers emit no sink event.
 * The browser SDK records the real `sinkchange` event and hashes its device id.
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

/** List output devices for the UI; labels stay local and sink ids are hashed before capture. */
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
