/**
 * OpenAI Realtime mode: earshot capture wrapped around a RAW OpenAI Realtime
 * WebRTC voice connection — no OpenAI SDK, just `RTCPeerConnection` and the SDP
 * exchange OpenAI documents.
 *
 * The handshake (browser-native path):
 *  1. capture the real microphone and add its track to an `RTCPeerConnection`;
 *  2. open the `oai-events` data channel and render the model's audio track
 *     through a real `AudioContext`;
 *  3. create an SDP offer, `setLocalDescription`, and POST the offer SDP to
 *     `https://api.openai.com/v1/realtime?model=…` with
 *     `Authorization: Bearer <runtime key>` and `Content-Type: application/sdp`;
 *  4. `setRemoteDescription` with the SDP answer OpenAI returns.
 *
 * Credential handling: the key is read from the runtime UI (recommended: an
 * ephemeral client secret minted server-side with `POST /v1/realtime/sessions`).
 * It is NEVER hardcoded, NEVER read from build-time env, and NEVER committed —
 * the example builds and typechecks with no key present. No automated test in
 * this package ever calls the OpenAI API.
 *
 * WebSocket vs WebRTC: OpenAI's WebSocket transport is intended for server-side
 * use (a browser cannot set the `Authorization` header on a WebSocket handshake).
 * From the browser the correct raw transport is WebRTC, which is what this does.
 */

import type { VoiceModeHandle } from "./mode.js";
import { realtimeSdpUrl, requireRealtimeKey } from "./realtime-url.js";
import type { CaptureSession } from "./session.js";
import { supportsSinkSelection } from "./sinks.js";

export interface OpenAiRealtimeOptions {
  /** The runtime key (ephemeral client secret recommended). Never committed. */
  apiKey: string;
  /** Realtime model id; falls back to the module default. */
  model?: string;
  /** Override the SDP-exchange base URL (e.g. a gateway). */
  baseUrl?: string;
  log: (message: string) => void;
}

/** Perform the SDP exchange with OpenAI and return the answer SDP. */
async function exchangeSdp(offerSdp: string, key: string, url: string): Promise<string> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/sdp",
    },
    body: offerSdp,
  });
  if (!response.ok) {
    // Do not echo the response body: an auth error can quote the request.
    throw new Error(`OpenAI Realtime SDP exchange failed (HTTP ${response.status}).`);
  }
  return response.text();
}

/**
 * Start OpenAI Realtime mode against an already-constructed {@link CaptureSession}.
 * The caller owns `session.start()`/`session.stop()`; this returns a handle that
 * tears down only the mode-specific media resources.
 */
export async function startOpenAiRealtime(
  session: CaptureSession,
  options: OpenAiRealtimeOptions,
): Promise<VoiceModeHandle> {
  const key = requireRealtimeKey(options.apiKey);
  const { log } = options;

  // Real Permissions API + devicechange, then the real getUserMedia mic path.
  await session.observeDevices();
  const micStream = await session.requestMicrophone({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  if (micStream === null) {
    throw new Error("Microphone permission was denied; cannot start OpenAI Realtime.");
  }

  const pc = new RTCPeerConnection();
  const audioCtx = new AudioContext();
  await audioCtx.resume();
  const monitor = audioCtx.createGain();
  monitor.gain.value = 1;
  monitor.connect(audioCtx.destination);

  // The model's audio arrives as a remote track; render it through the context.
  pc.addEventListener("track", (event) => {
    const stream = event.streams[0] ?? new MediaStream([event.track]);
    const source = audioCtx.createMediaStreamSource(stream);
    source.connect(monitor);
    log("remote audio track received; rendering through AudioContext");
  });
  pc.addEventListener("connectionstatechange", () => {
    log(`OpenAI peer connection: ${pc.connectionState}`);
  });

  // The Realtime event channel (session config, transcripts, tool calls, …).
  const events = pc.createDataChannel("oai-events");
  events.addEventListener("open", () => log("oai-events data channel open"));

  for (const track of micStream.getTracks()) {
    pc.addTrack(track, micStream);
  }

  // Observe the render context up front; outputLatency/render-queue become real
  // once the model's audio starts flowing (until then the SDK records coverage).
  session.attachAudioContext(audioCtx, 1000);
  session.attachPeerConnection(pc, 1000);

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const url = realtimeSdpUrl({ model: options.model, baseUrl: options.baseUrl });
  log(`POSTing SDP offer to ${url}`);
  const answerSdp = await exchangeSdp(offer.sdp ?? "", key, url);
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
  log("OpenAI Realtime connected; capture is live");

  return {
    audioCtx,
    supportsSinkSwitch: supportsSinkSelection(audioCtx),
    setMonitorGain: (value) => {
      monitor.gain.value = Math.max(0, Math.min(1, value));
    },
    stop: async () => {
      for (const track of micStream.getTracks()) track.stop();
      events.close();
      pc.close();
      await audioCtx.close();
    },
  };
}
