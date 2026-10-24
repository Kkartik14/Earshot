import { useId, useRef, useState, type FormEvent } from "react";
import { AnchorLink, type ViewerLinkComponent } from "../../navigation";
import {
  attachTranscript,
  createRecording,
  uploadAudio,
  UploadError,
  writeHeaders,
  type AttachedTranscript,
  type Recording,
  type UploadStep,
} from "./api";
import {
  AUDIO_ACCEPT,
  CHANNEL_LAYOUTS,
  EMPTY_UPLOAD,
  planUpload,
  TRANSCRIPT_ACCEPT,
  UPLOAD_FIELDS,
  type ChannelLayout,
  type UploadErrors,
  type UploadField,
  type UploadForm,
} from "./form";
import styles from "./UploadCall.module.css";

const STEP_STATUS: Record<UploadStep, string> = {
  create: "Creating the recording…",
  upload: "Uploading the audio…",
  transcript: "Attaching the transcript…",
};

type Phase =
  | { kind: "idle" }
  | { kind: "running"; step: UploadStep }
  | { kind: "failed"; message: string }
  | { kind: "done"; recording: Recording; transcript: AttachedTranscript | null };

/** One upload in progress. The same audio, layout and session key resume it, so a
 * step that finished (the recording, its audio) is not repeated after a failure;
 * anything else starts a new recording. */
interface Draft {
  audio: File;
  layout: ChannelLayout;
  sessionKeyInput: string;
  recordingId: string;
  sessionKey: string;
  startedAt: string;
  recording: Recording | null;
  uploaded: boolean;
}

/** 32 random hex digits (`crypto.randomUUID` needs a secure context; a self-hosted
 * viewer may be served over plain HTTP on a private network). */
function randomHex(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function failureMessage(error: unknown): string {
  if (!(error instanceof UploadError)) {
    return "Unable to reach the Earshot API. Check the connection and upload again.";
  }
  switch (error.code) {
    case "EARSHOT_PRESET_FORBIDS_MEDIA":
      return "This project's capture preset (metadata_only) stores no recordings, so the call was not uploaded.";
    case "EARSHOT_TRANSCRIPT_EXISTS":
      return "The audio is uploaded, but this recording already has a different transcript. Earshot keeps the first transcript and never replaces it.";
    case "EARSHOT_PRESET_FORBIDS_TRANSCRIPT":
      return "The audio is uploaded, but this project's capture preset (metadata_only) keeps no transcript text, so the transcript was not attached.";
  }
  switch (error.step) {
    case "create":
      return `The recording could not be created: ${error.message}.`;
    case "upload":
      return `The audio could not be uploaded: ${error.message}.`;
    case "transcript":
      return `The audio is uploaded, but the transcript was not attached: ${error.message}.`;
  }
}

function doneMessage(transcript: AttachedTranscript | null): string {
  if (transcript == null) return "Call uploaded: the audio is stored.";
  const count = transcript.line_count;
  const lines = count === 1 ? "1 line is" : `${count} lines are`;
  return transcript.timing === "measured"
    ? `Call uploaded: the audio is stored and the transcript's ${lines} attached, each with its time.`
    : `Call uploaded: the audio is stored and the transcript's ${lines} attached in order, without times.`;
}

/** "Upload a call": a recording's audio, optionally its transcript (WebVTT or plain
 * text), optionally the session key of the call it belongs to. Creates the
 * recording, uploads the audio, then attaches the transcript. */
export function UploadCall({
  LinkComponent = AnchorLink,
}: {
  LinkComponent?: ViewerLinkComponent;
}) {
  const id = useId();
  const [form, setForm] = useState<UploadForm>(EMPTY_UPLOAD);
  const [errors, setErrors] = useState<UploadErrors>({});
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  // Remounts the inputs to clear the chosen files ("Upload another call").
  const [generation, setGeneration] = useState(0);
  const draft = useRef<Draft | null>(null);
  const audioRef = useRef<HTMLInputElement>(null);
  const layoutRef = useRef<HTMLSelectElement>(null);
  const transcriptRef = useRef<HTMLInputElement>(null);
  const sessionKeyRef = useRef<HTMLInputElement>(null);
  const fieldRefs: Record<UploadField, { current: HTMLElement | null }> = {
    audio: audioRef,
    layout: layoutRef,
    transcript: transcriptRef,
    sessionKey: sessionKeyRef,
  };
  const running = phase.kind === "running";
  const fieldId = (field: UploadField) => `${id}-${field}`;
  const describedBy = (field: UploadField) =>
    errors[field]
      ? `${fieldId(field)}-hint ${fieldId(field)}-error`
      : `${fieldId(field)}-hint`;
  const invalid = (field: UploadField) => (errors[field] ? true : undefined);
  const fieldError = (field: UploadField) =>
    errors[field] ? (
      <span id={`${fieldId(field)}-error`} className={styles.fieldError}>
        {errors[field]}
      </span>
    ) : null;
  const change = <K extends UploadField>(field: K, value: UploadForm[K]) =>
    setForm((current) => ({ ...current, [field]: value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const { errors: found, plan } = planUpload(form);
    setErrors(found);
    if (plan == null) {
      const first = UPLOAD_FIELDS.find((field) => found[field] != null);
      if (first != null) fieldRefs[first].current?.focus();
      return;
    }
    let current = draft.current;
    if (
      current == null ||
      current.audio !== plan.audio ||
      current.layout !== form.layout ||
      current.sessionKeyInput !== form.sessionKey
    ) {
      current = {
        audio: plan.audio,
        layout: form.layout,
        sessionKeyInput: form.sessionKey,
        recordingId: `upload-${randomHex()}`,
        sessionKey: plan.sessionKey ?? `upload-${randomHex()}`,
        startedAt: (BigInt(Date.now()) * 1_000_000n).toString(),
        recording: null,
        uploaded: false,
      };
      draft.current = current;
    }
    setPhase({ kind: "running", step: "create" });
    try {
      const headers = await writeHeaders();
      if (current.recording == null) {
        current.recording = await createRecording(
          {
            recording_id: current.recordingId,
            session_key: current.sessionKey,
            speaker: "mixed",
            content_type: plan.contentType,
            started_at_unix_nano: current.startedAt,
            channel_speakers: plan.channelSpeakers,
          },
          headers,
        );
      }
      if (!current.uploaded) {
        setPhase({ kind: "running", step: "upload" });
        current.recording = await uploadAudio(current.recording, plan.audio, headers);
        current.uploaded = true;
      }
      let transcript: AttachedTranscript | null = null;
      if (plan.transcript != null) {
        setPhase({ kind: "running", step: "transcript" });
        transcript = await attachTranscript(
          current.recording.recording_id,
          plan.transcript.file,
          plan.transcript.contentType,
          headers,
        );
      }
      setPhase({ kind: "done", recording: current.recording, transcript });
    } catch (error) {
      setPhase({ kind: "failed", message: failureMessage(error) });
    }
  };

  const reset = () => {
    draft.current = null;
    setForm(EMPTY_UPLOAD);
    setErrors({});
    setPhase({ kind: "idle" });
    setGeneration((value) => value + 1);
  };

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 id={`${id}-title`} className={styles.title}>
          Upload a call
        </h1>
        <p className={styles.subtitle}>
          Earshot stores the recording, then reads its transcript as the call&apos;s
          transcript lines. Nothing leaves this Earshot server.
        </p>
      </header>

      <form
        key={generation}
        aria-labelledby={`${id}-title`}
        aria-busy={running ? true : undefined}
        className={styles.form}
        onSubmit={submit}
        noValidate
      >
        <fieldset className={styles.fields} disabled={running}>
          <div className={styles.field}>
            <label htmlFor={fieldId("audio")}>Call audio</label>
            <input
              ref={audioRef}
              id={fieldId("audio")}
              type="file"
              accept={AUDIO_ACCEPT}
              required
              aria-invalid={invalid("audio")}
              aria-describedby={describedBy("audio")}
              onChange={(event) => change("audio", event.target.files?.[0] ?? null)}
            />
            <span id={`${fieldId("audio")}-hint`} className={styles.hint}>
              WAV, FLAC, Ogg Opus, MP3 or WebM.
            </span>
            {fieldError("audio")}
          </div>

          <div className={styles.field}>
            <label htmlFor={fieldId("layout")}>Channels</label>
            <select
              ref={layoutRef}
              id={fieldId("layout")}
              value={form.layout}
              aria-invalid={invalid("layout")}
              aria-describedby={describedBy("layout")}
              onChange={(event) => change("layout", event.target.value as ChannelLayout)}
            >
              {CHANNEL_LAYOUTS.map((layout) => (
                <option key={layout.value} value={layout.value}>
                  {layout.label}
                </option>
              ))}
            </select>
            <span id={`${fieldId("layout")}-hint`} className={styles.hint}>
              Two channels need a WAV, FLAC or Ogg Opus file; Earshot checks the
              file&apos;s channel count.
            </span>
            {fieldError("layout")}
          </div>

          <div className={styles.field}>
            <label htmlFor={fieldId("transcript")}>Transcript (optional)</label>
            <input
              ref={transcriptRef}
              id={fieldId("transcript")}
              type="file"
              accept={TRANSCRIPT_ACCEPT}
              aria-invalid={invalid("transcript")}
              aria-describedby={describedBy("transcript")}
              onChange={(event) => change("transcript", event.target.files?.[0] ?? null)}
            />
            <span id={`${fieldId("transcript")}-hint`} className={styles.hint}>
              WebVTT (.vtt) keeps each line&apos;s time; plain text (.txt) keeps only
              their order. Name each line&apos;s speaker with &lt;v user&gt; or &lt;v
              agent&gt; in WebVTT, or start the line with user: or agent:.
            </span>
            {fieldError("transcript")}
          </div>

          <div className={styles.field}>
            <label htmlFor={fieldId("sessionKey")}>Session key (optional)</label>
            <input
              ref={sessionKeyRef}
              id={fieldId("sessionKey")}
              type="text"
              value={form.sessionKey}
              spellCheck={false}
              autoComplete="off"
              aria-invalid={invalid("sessionKey")}
              aria-describedby={describedBy("sessionKey")}
              onChange={(event) => change("sessionKey", event.target.value)}
            />
            <span id={`${fieldId("sessionKey")}-hint`} className={styles.hint}>
              To add this recording to a call Earshot already has, enter that call&apos;s
              session key. Leave it empty to start a new call.
            </span>
            {fieldError("sessionKey")}
          </div>

          <div className={styles.actions}>
            <button type="submit" className={styles.primary}>
              Upload call
            </button>
            {phase.kind === "done" ? (
              <button type="button" className={styles.secondary} onClick={reset}>
                Upload another call
              </button>
            ) : null}
          </div>
          <p className={styles.hint}>
            If a step fails, fix it and upload again: the steps that finished are not
            repeated.
          </p>
        </fieldset>
      </form>

      <div className={styles.outcome}>
        <p role="status" className={styles.status}>
          {phase.kind === "running"
            ? STEP_STATUS[phase.step]
            : phase.kind === "done"
              ? doneMessage(phase.transcript)
              : ""}
        </p>
        {phase.kind === "done" ? (
          <LinkComponent
            href={`/calls/${encodeURIComponent(phase.recording.call_id)}`}
            className={styles.callLink}
          >
            Open the call
          </LinkComponent>
        ) : null}
        {phase.kind === "failed" ? (
          <div role="alert" className={styles.failure}>
            {phase.message}
          </div>
        ) : null}
      </div>
    </div>
  );
}
