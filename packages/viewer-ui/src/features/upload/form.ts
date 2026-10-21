import type { components } from "../../api/schema";

type RecordingCreate = components["schemas"]["RecordingCreateRequest"];
export type AudioContentType = RecordingCreate["content_type"];
export type TranscriptContentType = "text/vtt" | "text/plain";
export type Speaker = "user" | "agent";
export type ChannelLayout = "mixed" | "user-agent" | "agent-user";

/** How the file's channels map to speakers: one mixed channel, or one per speaker. */
export const CHANNEL_LAYOUTS: readonly {
  value: ChannelLayout;
  label: string;
  speakers: Speaker[] | null;
}[] = [
  { value: "mixed", label: "One channel: both speakers mixed", speakers: null },
  {
    value: "user-agent",
    label: "Two channels: user left, agent right",
    speakers: ["user", "agent"],
  },
  {
    value: "agent-user",
    label: "Two channels: agent left, user right",
    speakers: ["agent", "user"],
  },
];

// What the recordings API stores, by the MIME types browsers report and by file
// extension (a browser may report none, or a vendor alias).
const AUDIO_BY_MIME: Record<string, AudioContentType> = {
  "audio/wav": "audio/wav",
  "audio/wave": "audio/wav",
  "audio/x-wav": "audio/wav",
  "audio/vnd.wave": "audio/wav",
  "audio/flac": "audio/flac",
  "audio/x-flac": "audio/flac",
  "audio/ogg": "audio/ogg",
  "audio/opus": "audio/ogg",
  "audio/mpeg": "audio/mpeg",
  "audio/mp3": "audio/mpeg",
  "audio/webm": "audio/webm",
};
const AUDIO_BY_EXTENSION: Record<string, AudioContentType> = {
  wav: "audio/wav",
  flac: "audio/flac",
  ogg: "audio/ogg",
  opus: "audio/ogg",
  mp3: "audio/mpeg",
  webm: "audio/webm",
};
// The formats whose header the server reads for its channel count: only these can
// carry a channel per speaker.
const CHANNEL_CHECKED = new Set<AudioContentType>([
  "audio/wav",
  "audio/flac",
  "audio/ogg",
]);
const TRANSCRIPT_BY_MIME: Record<string, TranscriptContentType> = {
  "text/vtt": "text/vtt",
  "text/plain": "text/plain",
};
const TRANSCRIPT_BY_EXTENSION: Record<string, TranscriptContentType> = {
  vtt: "text/vtt",
  txt: "text/plain",
};
// A session key is 1 to 256 bytes of UTF-8 after trimming (the server's rule).
const SESSION_KEY_MAX_BYTES = 256;

export const AUDIO_ACCEPT = [
  ...Object.keys(AUDIO_BY_MIME),
  ...Object.keys(AUDIO_BY_EXTENSION).map((extension) => `.${extension}`),
].join(",");
export const TRANSCRIPT_ACCEPT = ".vtt,.txt,text/vtt,text/plain";

function extension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

function mime(type: string): string {
  return type.split(";", 1)[0].trim().toLowerCase();
}

export function audioContentType(file: File): AudioContentType | null {
  return (
    AUDIO_BY_MIME[mime(file.type)] ?? AUDIO_BY_EXTENSION[extension(file.name)] ?? null
  );
}

export function transcriptContentType(file: File): TranscriptContentType | null {
  return (
    TRANSCRIPT_BY_MIME[mime(file.type)] ??
    TRANSCRIPT_BY_EXTENSION[extension(file.name)] ??
    null
  );
}

export interface UploadForm {
  audio: File | null;
  layout: ChannelLayout;
  transcript: File | null;
  sessionKey: string;
}

export type UploadField = keyof UploadForm;
export type UploadErrors = Partial<Record<UploadField, string>>;
/** The fields in the order the form shows them (focus goes to the first invalid). */
export const UPLOAD_FIELDS: readonly UploadField[] = [
  "audio",
  "layout",
  "transcript",
  "sessionKey",
];

export const EMPTY_UPLOAD: UploadForm = {
  audio: null,
  layout: "mixed",
  transcript: null,
  sessionKey: "",
};

/** What one upload sends, once the form is valid. */
export interface UploadPlan {
  audio: File;
  contentType: AudioContentType;
  channelSpeakers: Speaker[] | null;
  transcript: { file: File; contentType: TranscriptContentType } | null;
  /** The trimmed key, or null to start a new call under a key of the form's own. */
  sessionKey: string | null;
}

export function planUpload(form: UploadForm): {
  errors: UploadErrors;
  plan: UploadPlan | null;
} {
  const errors: UploadErrors = {};
  const contentType = form.audio == null ? null : audioContentType(form.audio);
  if (form.audio == null) errors.audio = "Choose the call's audio file.";
  else if (contentType == null)
    errors.audio = "Choose a WAV, FLAC, Ogg Opus, MP3 or WebM file.";
  const speakers = CHANNEL_LAYOUTS.find(
    (layout) => layout.value === form.layout,
  )?.speakers;
  if (speakers != null && contentType != null && !CHANNEL_CHECKED.has(contentType)) {
    errors.layout = "Two channels need a WAV, FLAC or Ogg Opus file.";
  }
  const transcriptType =
    form.transcript == null ? null : transcriptContentType(form.transcript);
  if (form.transcript != null && transcriptType == null) {
    errors.transcript = "Choose a WebVTT (.vtt) or plain text (.txt) file.";
  }
  const sessionKey = form.sessionKey.trim();
  if (new TextEncoder().encode(sessionKey).length > SESSION_KEY_MAX_BYTES) {
    errors.sessionKey = `Use at most ${SESSION_KEY_MAX_BYTES} bytes.`;
  }
  if (Object.keys(errors).length > 0 || form.audio == null || contentType == null) {
    return { errors, plan: null };
  }
  return {
    errors,
    plan: {
      audio: form.audio,
      contentType,
      channelSpeakers: speakers ?? null,
      transcript:
        form.transcript == null || transcriptType == null
          ? null
          : { file: form.transcript, contentType: transcriptType },
      sessionKey: sessionKey === "" ? null : sessionKey,
    },
  };
}
