/**
 * @earshot/browser — the client-side voice capture kernel.
 *
 * Captures `getUserMedia`/`RTCPeerConnection`/`AudioContext` telemetry and a
 * W3C trace-context, emits a versioned `CapturePayload` in the exact shape the
 * server engines (`analyze_webrtc_stats`, `analyze_audio_graph`) consume, and
 * delivers it to the backend's `POST /v1/capture` endpoint.
 *
 * STATUS: the mapping, bounding and delivery logic is unit-tested against mocked
 * W3C APIs and a mocked `fetch`; it has NOT yet been run against a real browser
 * / WebRTC / Web Audio runtime. See README.md for exactly what that leaves open.
 */

export {
  EarshotBrowserRecorder,
  createBrowserRecorder,
  type AttachAudioContextOptions,
  type AttachPeerConnectionOptions,
  type BrowserRecorderOptions,
  type ObserveMediaDevicesOptions,
} from "./recorder.js";

export {
  EarshotCaptureTransport,
  createCaptureTransport,
  type CaptureCoverageSink,
  type CaptureDeliveryFailure,
  type CaptureDeliveryResult,
  type CaptureRequestInit,
  type CaptureResponseLike,
  type CaptureTransportOptions,
  type FetchLike,
} from "./transport.js";

export { CAPTURE_PROTOCOL_VERSION } from "./protocol.js";

export { normalizeStatsReport } from "./webrtc.js";
export {
  audioContextStateEvent,
  classifyPermissionError,
  deviceChangeEvent,
  latencyEvent,
  permissionEvent,
  renderQueueSeconds,
  sampleRateMismatchEvent,
  sinkChangeEvent,
  sinkIdToString,
  underrunEvent,
} from "./device.js";
export {
  createTraceContext,
  injectTraceHeaders,
  parseTraceParent,
} from "./trace-context.js";
export { opaqueDeviceId, makeSalt } from "./privacy.js";

export type {
  AudioContextLike,
  AudioTimestampLike,
  BrowserClockDomain,
  CaptureCoverage,
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
