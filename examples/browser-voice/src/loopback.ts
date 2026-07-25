/**
 * Loopback mode: a REAL WebRTC session that stays on your machine.
 *
 * It captures the real microphone and pipes it through two real
 * `RTCPeerConnection`s (a sender and a receiver, connected by host ICE
 * candidates), then renders the received audio through a real `AudioContext`.
 * No external service and no API key are involved, so this is the mode you can
 * run anywhere to exercise the capture code paths end to end.
 *
 * What it drives that a synthesised-source loopback cannot:
 *  - the real `getUserMedia` microphone-permission path (via the session);
 *  - real `inbound-rtp` audio stats on the RECEIVER's `getStats()`;
 *  - a real `AudioContext` output path (state / outputLatency / render-queue /
 *    sinkchange), because the received stream is actually connected to the
 *    context's output device.
 *
 * The received audio is routed through a gain node defaulting to 0 (silent) so
 * running it near your microphone does not howl; the render path is still live
 * (the context runs and outputs to its device), so `outputLatency` and
 * `getOutputTimestamp()` are real. Raise the monitor gain (headphones advised) to
 * actually hear it.
 */

import type { VoiceModeHandle } from "./mode.js";
import type { CaptureSession } from "./session.js";
import { supportsSinkSelection } from "./sinks.js";

/** Wire two RTCPeerConnections in a local loopback, exchanging host candidates. */
async function connectLoopback(
  micStream: MediaStream,
  log: (message: string) => void,
): Promise<{
  sender: RTCPeerConnection;
  receiver: RTCPeerConnection;
  remote: MediaStream;
}> {
  const sender = new RTCPeerConnection();
  const receiver = new RTCPeerConnection();

  sender.addEventListener("icecandidate", (event) => {
    if (event.candidate) void receiver.addIceCandidate(event.candidate);
  });
  receiver.addEventListener("icecandidate", (event) => {
    if (event.candidate) void sender.addIceCandidate(event.candidate);
  });
  receiver.addEventListener("connectionstatechange", () => {
    log(`loopback receiver connection: ${receiver.connectionState}`);
  });

  const remote = new MediaStream();
  receiver.addEventListener("track", (event) => {
    remote.addTrack(event.track);
  });

  for (const track of micStream.getTracks()) {
    sender.addTrack(track, micStream);
  }

  const offer = await sender.createOffer();
  await sender.setLocalDescription(offer);
  await receiver.setRemoteDescription(offer);
  const answer = await receiver.createAnswer();
  await receiver.setLocalDescription(answer);
  await sender.setRemoteDescription(answer);

  return { sender, receiver, remote };
}

/**
 * Start loopback mode against an already-constructed {@link CaptureSession}. The
 * caller is responsible for `session.start()`/`session.stop()`; this returns a
 * handle that tears down only the mode-specific media resources.
 */
export async function startLoopback(
  session: CaptureSession,
  log: (message: string) => void,
): Promise<VoiceModeHandle> {
  // Real Permissions API + devicechange, then the real getUserMedia mic path.
  await session.observeDevices();
  const micStream = await session.requestMicrophone({ audio: true });
  if (micStream === null) {
    throw new Error("Microphone permission was denied; cannot start loopback.");
  }

  const { sender, receiver, remote } = await connectLoopback(micStream, log);
  // Sample the RECEIVER: its getStats() carries the inbound-rtp audio + playout
  // stats the server's analyze_webrtc_stats engine reads.
  session.attachPeerConnection(receiver, 1000);

  const audioCtx = new AudioContext();
  await audioCtx.resume();
  const source = audioCtx.createMediaStreamSource(remote);
  const monitor = audioCtx.createGain();
  monitor.gain.value = 0; // silent by default to avoid feedback
  source.connect(monitor);
  monitor.connect(audioCtx.destination);
  session.attachAudioContext(audioCtx, 1000);

  log(
    `loopback live: mic -> sender -> receiver -> AudioContext ` +
      `(sampleRate=${audioCtx.sampleRate}Hz, ` +
      `outputLatency=${audioCtx.outputLatency ?? "unavailable"})`,
  );

  return {
    audioCtx,
    supportsSinkSwitch: supportsSinkSelection(audioCtx),
    setMonitorGain: (value) => {
      monitor.gain.value = Math.max(0, Math.min(1, value));
    },
    stop: async () => {
      for (const track of micStream.getTracks()) track.stop();
      sender.close();
      receiver.close();
      await audioCtx.close();
    },
  };
}
