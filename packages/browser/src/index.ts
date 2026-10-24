/** Client-side voice capture for WebRTC and Web Audio, delivered as versioned payloads to `POST /v1/capture`. */

export {
  EarshotBrowserRecorder,
  createBrowserRecorder,
  type AttachAudioContextOptions,
  type AttachPeerConnectionOptions,
  type BrowserRecorderOptions,
  type DrainOptions,
  type ObserveMediaDevicesOptions,
} from "./recorder.js";

export {
  EarshotCaptureTransport,
  createCaptureTransport,
  type CaptureAcknowledgement,
  type CaptureCoverageSink,
  type CaptureDeliveryFailure,
  type CaptureDeliveryResult,
  type CaptureFlushRetryOutcome,
  type CaptureFlushResult,
  type CaptureRequestInit,
  type CaptureResponseLike,
  type CaptureTransportOptions,
  type FetchLike,
} from "./transport.js";

export {
  CAPTURE_PROTOCOL_VERSION,
  CONTINUOUS_CAPTURE_VERSION,
  type CaptureVersion,
} from "./protocol.js";

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
  CaptureEnd,
  CaptureEndReason,
  CapturePayload,
  CaptureResync,
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
