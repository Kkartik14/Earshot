import { z } from "zod";

/**
 * Voice Agent Trace Format — v0.
 *
 * The canonical, vendor-neutral contract for a voice-agent conversation. The SDK
 * emits this; the backend validates and derives from it. See
 * `docs/trace-format.md` for the prose spec and design principles.
 *
 * Rule of thumb encoded here: the SDK emits FACTS (raw timings, events). Derived
 * metrics (TTFT, TTFB, latency, rates) are computed by the backend and are NOT
 * part of this schema — so a producer cannot report them wrong.
 */

export const SCHEMA_VERSION = "0.1" as const;

/** Milliseconds from session start — the call-relative monotonic clock. */
const Millis = z.number().finite().nonnegative();
/** An opaque, non-empty identifier. */
const Id = z.string().min(1);

// ---------------------------------------------------------------------------
// Enums (values are data, not schema — providers/frameworks are free strings)
// ---------------------------------------------------------------------------

export const SessionStatus = z.enum(["completed", "failed", "abandoned"]);
export const TurnStatus = z.enum(["completed", "interrupted", "no_response", "failed"]);
export const SpanType = z.enum(["stt", "llm", "tool", "tts", "playout"]);
export const SpanStatus = z.enum(["ok", "error", "cancelled", "timeout"]);
export const InterruptionType = z.enum(["barge_in", "dtmf"]);
export const AudioKind = z.enum(["input", "output"]);
export const Framework = z.enum(["pipecat", "livekit", "vapi", "custom"]);

// ---------------------------------------------------------------------------
// Shared
// ---------------------------------------------------------------------------

export const TraceErrorSchema = z.object({
  code: z.string().min(1),
  category: z.string().min(1),
  message: z.string(),
});

// ---------------------------------------------------------------------------
// Audio (by reference — never inline)
// ---------------------------------------------------------------------------

export const AudioFormatSchema = z.object({
  encoding: z.string().min(1),
  sampleRate: z.number().int().positive(),
  channels: z.number().int().positive(),
});

export const AudioRefSchema = z.object({
  ref: Id,
  kind: AudioKind,
  turnId: Id.optional(),
  uri: z.string().min(1),
  format: AudioFormatSchema,
  startMs: Millis,
  durationMs: Millis,
  byteRange: z
    .tuple([z.number().int().nonnegative(), z.number().int().nonnegative()])
    .optional(),
});

// ---------------------------------------------------------------------------
// Span (waterfall rows)
// ---------------------------------------------------------------------------

export const SpanSchema = z.object({
  spanId: Id,
  turnId: Id,
  type: SpanType,
  name: z.string().optional(),
  provider: z.string().optional(),
  startMs: Millis,
  endMs: Millis,
  status: SpanStatus,
  /** Type-specific, conventioned but open. See docs/trace-format.md. */
  attributes: z.record(z.unknown()).default({}),
  error: TraceErrorSchema.nullable().default(null),
});

// ---------------------------------------------------------------------------
// Event (optional, fine-grained timeline)
// ---------------------------------------------------------------------------

export const EventSchema = z.object({
  eventId: Id,
  turnId: Id,
  spanId: Id.optional(),
  /** Reserved types documented in the spec; any string is accepted (additive). */
  type: z.string().min(1),
  tMs: Millis,
  data: z.record(z.unknown()).default({}),
});

// ---------------------------------------------------------------------------
// Turn
// ---------------------------------------------------------------------------

export const UtteranceSchema = z.object({
  transcript: z.string(),
  startMs: Millis,
  endMs: Millis,
});

export const InterruptionSchema = z.object({
  type: InterruptionType,
  atMs: Millis,
  wasSpeaking: z.boolean(),
});

export const TurnSchema = z.object({
  turnId: Id,
  sessionId: Id,
  index: z.number().int().nonnegative(),
  /** `endMs` is the end-of-utterance anchor most latency metrics measure from. */
  user: UtteranceSchema.optional(),
  agent: UtteranceSchema.optional(),
  status: TurnStatus,
  interruption: InterruptionSchema.nullable().default(null),
  error: TraceErrorSchema.nullable().default(null),
  audio: z.object({ inputRef: Id.optional(), outputRef: Id.optional() }).optional(),
});

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export const AgentInfoSchema = z.object({
  id: Id,
  name: z.string().optional(),
  version: z.string().optional(),
});

export const SourceInfoSchema = z.object({
  framework: Framework,
  sdkVersion: z.string().optional(),
});

/** Known provider slots, but any additional slot is allowed. */
export const ProvidersSchema = z
  .object({
    transport: z.string().optional(),
    stt: z.string().optional(),
    llm: z.string().optional(),
    tts: z.string().optional(),
  })
  .catchall(z.string());

export const ParticipantSchema = z.object({
  role: z.string().optional(),
  channel: z.string().optional(),
  idHash: z.string().optional(),
});

export const ConsentSchema = z.object({
  recording: z.boolean().default(false),
  piiRedacted: z.boolean().default(false),
});

export const SessionSchema = z.object({
  schemaVersion: z.literal(SCHEMA_VERSION),
  sessionId: Id,
  agent: AgentInfoSchema,
  source: SourceInfoSchema,
  providers: ProvidersSchema.default({}),
  participant: ParticipantSchema.optional(),
  startedAt: z.string().datetime(),
  endedAt: z.string().datetime().optional(),
  durationMs: Millis.optional(),
  status: SessionStatus,
  consent: ConsentSchema.default({ recording: false, piiRedacted: false }),
  metadata: z.record(z.unknown()).default({}),
});

// ---------------------------------------------------------------------------
// Bundle — the unit that gets uploaded for one call
// ---------------------------------------------------------------------------

export const TraceBundleSchema = z.object({
  schemaVersion: z.literal(SCHEMA_VERSION),
  session: SessionSchema,
  turns: z.array(TurnSchema).default([]),
  spans: z.array(SpanSchema).default([]),
  events: z.array(EventSchema).default([]),
  audio: z.array(AudioRefSchema).default([]),
});

// ---------------------------------------------------------------------------
// Inferred types
// ---------------------------------------------------------------------------

export type TraceError = z.infer<typeof TraceErrorSchema>;
export type AudioFormat = z.infer<typeof AudioFormatSchema>;
export type AudioRef = z.infer<typeof AudioRefSchema>;
export type Span = z.infer<typeof SpanSchema>;
export type Event = z.infer<typeof EventSchema>;
export type Utterance = z.infer<typeof UtteranceSchema>;
export type Interruption = z.infer<typeof InterruptionSchema>;
export type Turn = z.infer<typeof TurnSchema>;
export type AgentInfo = z.infer<typeof AgentInfoSchema>;
export type SourceInfo = z.infer<typeof SourceInfoSchema>;
export type Providers = z.infer<typeof ProvidersSchema>;
export type Participant = z.infer<typeof ParticipantSchema>;
export type Consent = z.infer<typeof ConsentSchema>;
export type Session = z.infer<typeof SessionSchema>;
export type TraceBundle = z.infer<typeof TraceBundleSchema>;

// ---------------------------------------------------------------------------
// Parse helpers (used by ingest — disk/network data is never trusted)
// ---------------------------------------------------------------------------

export type ParseResult =
  | { readonly ok: true; readonly bundle: TraceBundle }
  | { readonly ok: false; readonly error: z.ZodError };

/** Validate an untrusted value as a trace bundle, without throwing. */
export function parseTraceBundle(value: unknown): ParseResult {
  const result = TraceBundleSchema.safeParse(value);
  return result.success
    ? { ok: true, bundle: result.data }
    : { ok: false, error: result.error };
}
