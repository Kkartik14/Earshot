import { getViewerSession } from "../../api/auth";
import { apiUrl, notifyViewerSessionInvalid } from "../../api/client";
import type { components } from "../../api/schema";
import { mediaUrl } from "../calls/api";
import type { TranscriptContentType } from "./form";

export type Recording = components["schemas"]["RecordingResponse"];
export type RecordingManifest = components["schemas"]["RecordingCreateRequest"];
export type AttachedTranscript = components["schemas"]["RecordingTranscriptResponse"];

/** The three requests of an upload, in order. */
export type UploadStep = "create" | "upload" | "transcript";

/** A refused request: its step, status, and the server's stable code and message. */
export class UploadError extends Error {
  constructor(
    readonly step: UploadStep,
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "UploadError";
  }
}

export type WriteHeaders = Record<string, string>;

/** The headers a write needs: the viewer session's CSRF token, when the viewer has
 * a session (an anonymous loopback viewer has none and needs none). */
export async function writeHeaders(): Promise<WriteHeaders> {
  const session = await getViewerSession();
  if (session == null) {
    notifyViewerSessionInvalid();
    throw new UploadError(
      "create",
      401,
      "EARSHOT_UNAUTHORIZED",
      "viewer session required",
    );
  }
  return session.csrf_token == null ? {} : { "X-Earshot-CSRF": session.csrf_token };
}

async function send<T>(step: UploadStep, url: string, init: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    ...init,
  });
  if (!response.ok) {
    if (response.status === 401) notifyViewerSessionInvalid();
    const body = (await response.json().catch(() => null)) as {
      error?: { code?: string; message?: string };
    } | null;
    throw new UploadError(
      step,
      response.status,
      body?.error?.code ?? `HTTP ${response.status}`,
      body?.error?.message ?? `HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

/** Create the recording (the same manifest again answers the same recording). */
export function createRecording(
  manifest: RecordingManifest,
  headers: WriteHeaders,
): Promise<Recording> {
  return send("create", apiUrl("/v1/recordings"), {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(manifest),
  });
}

/** Upload the audio to the path the server gave (the same bytes again answer 200). */
export function uploadAudio(
  recording: Recording,
  audio: Blob,
  headers: WriteHeaders,
): Promise<Recording> {
  const url = mediaUrl(recording.upload_path);
  if (url == null) {
    return Promise.reject(
      new UploadError(
        "upload",
        0,
        "EARSHOT_UPLOAD_PATH_INVALID",
        "the upload path is not an API path",
      ),
    );
  }
  return send("upload", url, {
    method: "PUT",
    headers: { ...headers, "Content-Type": recording.content_type },
    body: audio,
  });
}

/** Attach the transcript (the same transcript again answers 200; another is 409). */
export function attachTranscript(
  recordingId: string,
  transcript: Blob,
  contentType: TranscriptContentType,
  headers: WriteHeaders,
): Promise<AttachedTranscript> {
  return send(
    "transcript",
    apiUrl(`/v1/recordings/${encodeURIComponent(recordingId)}/transcript`),
    {
      method: "POST",
      headers: { ...headers, "Content-Type": `${contentType}; charset=utf-8` },
      body: transcript,
    },
  );
}
