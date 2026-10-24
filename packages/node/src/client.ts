import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { unescape } from "node:querystring";
import { Readable } from "node:stream";
import type { components } from "./openapi.js";

type Schemas = components["schemas"];
export type Recording = Schemas["RecordingResponse"];
export type CallDetail = Schemas["CallDetailResponse"];
export type CallResolution = Schemas["CallResolveResponse"];
type Speakers = Schemas["RecordingCreateRequest"]["channel_speakers"];
/** A recording's manifest; `started_at_unix_nano` also takes a `bigint`. */
export type RecordingManifest = Omit<
  Schemas["RecordingCreateRequest"],
  "started_at_unix_nano" | "channel_speakers"
> & {
  started_at_unix_nano: string | bigint;
  channel_speakers?: readonly NonNullable<Speakers>[number][] | null;
};
type Environment = Readonly<Record<string, string | undefined>>;

const UNSAFE = /[^A-Za-z0-9._-]+/g;
const RECORDING_ID_MAX = 128;

/**
 * A request Earshot refused, one it redirected (never followed), one whose answer is
 * not JSON, or one that got no whole answer (`status` 0, `code` null). `detail` is
 * Earshot's message (Python's `EarshotError.message`); a JavaScript error's
 * `message` is its summary line.
 */
export class EarshotError extends Error {
  constructor(
    readonly method: string,
    readonly path: string,
    readonly status: number,
    readonly code: string | null,
    readonly detail: string | null,
  ) {
    super(`${method} ${path}: HTTP ${status} ${code ?? "without an error code"}`);
    this.name = "EarshotError";
  }
}

/**
 * A stable ID for the recording `name` of a session key's call, so a retried create
 * or upload names the same recording; equal to the Python client's `recording_id_for`.
 * Characters an ID cannot hold become `-`; an ID that changed, or is too long, keeps
 * a prefix plus a hash of the whole key, so distinct keys never share one.
 */
export function recordingIdFor(sessionKey: string, name: string): string {
  const suffix = `.${name}`;
  let base = sessionKey.replace(UNSAFE, "-").replace(/^[._-]+/, "") || "call";
  const room = RECORDING_ID_MAX - suffix.length;
  if (base.length > room || base !== sessionKey) {
    const digest = createHash("sha256").update(sessionKey).digest("hex").slice(0, 16);
    base = `${base.slice(0, room - digest.length - 1)}-${digest}`;
  }
  return base + suffix;
}

function authorizationFrom(environment: Environment): string | undefined {
  const key = environment.EARSHOT_API_KEY?.trim();
  if (key) return `Bearer ${key}`;
  for (const entry of (environment.OTEL_EXPORTER_OTLP_HEADERS ?? "").split(",")) {
    const [name, ...value] = entry.split("=");
    if (value.length > 0 && name?.trim().toLowerCase() === "authorization") {
      // Percent-decoded as the OTLP exporter reads it; a malformed escape stays as sent.
      return unescape(value.join("=").trim()) || undefined;
    }
  }
  return undefined;
}

/** The code and message of Earshot's error body, `{ error: { code, message } }`. */
function problemOf(text: string): [string | null, string | null] {
  let problem: Record<string, unknown> | undefined;
  try {
    problem = (JSON.parse(text) as { error?: Record<string, unknown> }).error;
  } catch {
    // Not Earshot's body: a proxy's error page, say.
  }
  const { code, message } = problem ?? {};
  return [
    typeof code === "string" ? code : null,
    typeof message === "string" ? message : null,
  ];
}

async function digestOf(source: string | URL | Uint8Array): Promise<[string, number]> {
  const hash = createHash("sha256");
  let size = 0;
  const blocks: Iterable<Uint8Array> | AsyncIterable<Uint8Array> =
    source instanceof Uint8Array ? [source] : createReadStream(source);
  for await (const block of blocks) {
    hash.update(block);
    size += block.length;
  }
  return [`sha-256=:${hash.digest("base64")}:`, size];
}

const json = (value: unknown): RequestInit => ({
  body: JSON.stringify(value),
  headers: { "Content-Type": "application/json" },
});

export interface EarshotClientOptions {
  /** The server's base URL: the same URL the OTLP exporter sends to. */
  baseUrl: string | URL;
  /** A project API key, sent as `Authorization: Bearer <key>`. */
  apiKey?: string;
  /** A whole `Authorization` header value, instead of `apiKey`. */
  authorization?: string;
  /** Abort any request that takes longer, an upload included. Default: none. */
  timeoutMs?: number;
}

/**
 * The recordings and calls API of one Earshot server, as one project's API key. It
 * never follows a redirect, so the key is only ever sent to `baseUrl`.
 */
export class EarshotClient {
  readonly baseUrl: string;
  readonly #authorization: string | undefined;
  readonly #timeoutMs: number | undefined;

  constructor(options: EarshotClientOptions) {
    if (options.apiKey !== undefined && options.authorization !== undefined) {
      throw new TypeError("pass apiKey or authorization, not both");
    }
    this.baseUrl = String(options.baseUrl).replace(/\/+$/, "");
    this.#authorization =
      options.apiKey !== undefined ? `Bearer ${options.apiKey}` : options.authorization;
    this.#timeoutMs = options.timeoutMs;
  }

  /**
   * A client for `EARSHOT_URL`, else `OTEL_EXPORTER_OTLP_ENDPOINT`, with the key in
   * `EARSHOT_API_KEY`, else the `Authorization` entry of `OTEL_EXPORTER_OTLP_HEADERS`
   * (the one the OTLP exporter already sends); with neither, anonymous (loopback).
   */
  static fromEnvironment(
    environment: Environment = process.env,
    options: Pick<EarshotClientOptions, "timeoutMs"> = {},
  ): EarshotClient {
    const baseUrl = environment.EARSHOT_URL || environment.OTEL_EXPORTER_OTLP_ENDPOINT;
    if (!baseUrl) throw new TypeError("set EARSHOT_URL or OTEL_EXPORTER_OTLP_ENDPOINT");
    const authorization = authorizationFrom(environment);
    return new EarshotClient({ ...options, baseUrl, authorization });
  }

  /**
   * Create (or find again) a pending recording of the call `session_key` names; it
   * holds the call open until uploaded. A `mixed` stereo recording names its
   * channels' speakers in `channel_speakers`. With a `recording_id` the request is
   * idempotent; without one the server mints one.
   */
  createRecording(manifest: RecordingManifest): Promise<Recording> {
    const started_at_unix_nano = String(manifest.started_at_unix_nano);
    return this.#send(
      "POST",
      "/v1/recordings",
      json({ ...manifest, started_at_unix_nano }),
    );
  }

  getRecording(recordingId: string): Promise<Recording> {
    return this.#send("GET", `/v1/recordings/${encodeURIComponent(recordingId)}`);
  }

  /**
   * Upload a pending recording's bytes, from a file path (streamed, never held whole
   * in memory) or from bytes, with their `Content-Digest`; the stored recording.
   * `recording` is what `createRecording` returned, or its ID. Uploading the same
   * bytes again answers the stored recording.
   */
  async uploadRecording(
    recording: Recording | string,
    source: string | URL | Uint8Array,
  ): Promise<Recording> {
    const { upload_path, content_type } =
      typeof recording === "string" ? await this.getRecording(recording) : recording;
    const [digest, size] = await digestOf(source);
    const body =
      source instanceof Uint8Array ? source : Readable.toWeb(createReadStream(source));
    const headers = { "Content-Length": String(size), "Content-Digest": digest };
    return this.#send("PUT", upload_path, {
      body: body as RequestInit["body"],
      headers: { ...headers, "Content-Type": content_type },
      duplex: "half",
    });
  }

  /** The call `sessionKey` names now: `{ call_id }`. */
  resolveCall(sessionKey: string): Promise<CallResolution> {
    return this.#send("POST", "/v1/calls:resolve", json({ session_key: sessionKey }));
  }

  /** The call's detail: revisions, recordings, transcript and, once computed, projection. */
  getCall(callId: string): Promise<CallDetail> {
    return this.#send("GET", `/v1/calls/${encodeURIComponent(callId)}`);
  }

  async #send<T>(method: string, path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (this.#authorization) headers.set("Authorization", this.#authorization);
    const timeout = this.#timeoutMs;
    const signal = timeout === undefined ? null : AbortSignal.timeout(timeout);
    let status: number;
    let text: string;
    try {
      const reply = await fetch(this.baseUrl + path, {
        ...init,
        method,
        headers,
        signal,
        // Following would send the key on to a URL the caller never chose.
        redirect: "manual",
      });
      [status, text] = [reply.status, await reply.text()];
    } catch (error) {
      // No answer: refused, reset or timed out. Name the cause, never the request.
      const cause = (error as { cause?: { code?: unknown } }).cause?.code;
      throw new EarshotError(
        method,
        path,
        0,
        null,
        String(cause ?? (error as Error).name),
      );
    }
    // A refusal, or a redirect (never followed).
    if (status < 200 || status >= 300) {
      throw new EarshotError(method, path, status, ...problemOf(text));
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      // Not Earshot's answer: a proxy's page, say.
      throw new EarshotError(method, path, status, null, "the answer is not JSON");
    }
  }
}
