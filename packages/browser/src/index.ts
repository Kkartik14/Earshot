/**
 * @earshot/browser — the client-side voice capture kernel.
 *
 * Captures `getUserMedia`/`RTCPeerConnection`/`AudioContext` telemetry and a
 * W3C trace-context, and emits a `CapturePayload` in the exact shape the
 * server engines (`analyze_webrtc_stats`, `analyze_audio_graph`) consume.
 *
 * STATUS: scaffolding. The mapping logic is unit-tested against mocked W3C APIs
 * but is NOT yet validated in a real browser / WebRTC runtime. See README.md.
 */

export {
  EarshotBrowserRecorder,
  createBrowserRecorder,
  type BrowserRecorderOptions,
  type AttachPeerConnectionOptions,
  type ObserveMediaDevicesOptions,
} from "./recorder.js";

export { normalizeStatsReport } from "./webrtc.js";
export {
  audioContextStateEvent,
  classifyPermissionError,
  deviceChangeEvent,
  latencyEvent,
  permissionEvent,
  sampleRateMismatchEvent,
  sinkChangeEvent,
  sinkIdToString,
  underrunEvent,
} from "./device.js";
export { createTraceContext, injectTraceHeaders } from "./trace-context.js";
export { opaqueDeviceId, makeSalt } from "./privacy.js";

export type {
  AudioContextLike,
  CapturePayload,
  Clock,
  DeviceEvent,
  EventTargetLike,
  MediaDeviceInfoLike,
  MediaDevicesLike,
  MediaStreamLike,
  MediaTrackLike,
  PeerConnectionLike,
  PermissionsLike,
  PermissionStatusLike,
  RandomSource,
  RTCStatsReportLike,
  Scheduler,
  StatMembers,
  TraceContext,
  WebRtcSnapshot,
} from "./types.js";
