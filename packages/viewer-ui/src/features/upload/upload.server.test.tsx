/// <reference types="node" />
import { readFileSync } from "node:fs";
import { File as PlatformFile } from "node:buffer";
import { join, resolve } from "node:path";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { startRealServer, type RealServer } from "../../test/realServer";
import { UploadCall } from "./UploadCall";

// The "Upload a call" form against a real Earshot server (`__fixtures__/serve_upload.py`:
// the real app over real SQLite and a local blob store). Every request is real HTTP,
// and what the form says is checked against what the server then serves.
//
// The audio is real: the Pipecat 1.5 capture's mono user track (WAV) and the LiveKit
// 1.8 capture's stereo Ogg Opus. Files are Node's platform `File` (a real Blob that
// fetch streams, as a browser's is); jsdom's own `File` is not one fetch can send.

interface Seed {
  port: number;
  control_port: number;
}

const ROOT = resolve(import.meta.dirname, "../../../../..");
const SCRIPT = join(import.meta.dirname, "__fixtures__", "serve_upload.py");
const MONO_WAV = join(ROOT, "fixtures/captured/pipecat-otlp-1.5/audio/user.wav");
const STEREO_OGG = join(ROOT, "fixtures/captured/livekit-otlp-1.8/audio/recording.ogg");
const WAIT = { timeout: 30_000 };

const VTT = `WEBVTT

00:01.250 --> 00:03.000
<v user>I'd like to book a table.

00:03.500 --> 00:05.000
<v agent>For how many people?
`;

let server: RealServer<Seed>;

beforeAll(async () => {
  server = await startRealServer<Seed>(SCRIPT);
}, 120_000);

afterAll(async () => {
  await server?.stop();
}, 60_000);

function platformFile(contents: string | Buffer, name: string, type: string): File {
  const part = typeof contents === "string" ? contents : new Uint8Array(contents);
  return new PlatformFile([part], name, { type }) as unknown as File;
}

const monoWav = () => platformFile(readFileSync(MONO_WAV), "user.wav", "audio/wav");

function choose(label: string, chosen: File) {
  fireEvent.change(screen.getByLabelText(label), { target: { files: [chosen] } });
}

function setText(name: string, value: string) {
  fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } });
}

const submit = () => fireEvent.click(screen.getByRole("button", { name: "Upload call" }));

async function setPreset(preset: "full" | "metadata_only") {
  const reply = await fetch(
    `http://127.0.0.1:${server.seed.control_port}/preset/${preset}`,
    {
      method: "POST",
    },
  );
  expect(reply.status).toBe(204);
}

interface CallDetail {
  session_key: string | null;
  span_count: number;
  recordings: {
    state: string;
    channel_speakers: string[] | null;
    content_type: string;
  }[];
}

/** The call the success message links to, as the server serves it. */
async function linkedCall(): Promise<CallDetail> {
  const link = await screen.findByRole("link", { name: "Open the call" }, WAIT);
  const href = link.getAttribute("href") ?? "";
  expect(href).toMatch(/^\/calls\/call_[0-9a-f]{32}$/);
  const reply = await fetch(`/v1${href}`);
  expect(reply.status).toBe(200);
  return (await reply.json()) as CallDetail;
}

async function callBySessionKey(sessionKey: string): Promise<CallDetail> {
  const resolved = await fetch("/v1/calls:resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_key: sessionKey }),
  });
  const { call_id } = (await resolved.json()) as { call_id: string };
  return (await (await fetch(`/v1/calls/${call_id}`)).json()) as CallDetail;
}

describe("Upload a call on a real server", () => {
  it("uploads a stereo call with its WebVTT transcript as a new call", async () => {
    render(<UploadCall />);
    choose("Call audio", platformFile(readFileSync(STEREO_OGG), "call.ogg", "audio/ogg"));
    fireEvent.change(screen.getByRole("combobox", { name: "Channels" }), {
      target: { value: "user-agent" },
    });
    choose("Transcript (optional)", platformFile(VTT, "call.vtt", "text/vtt"));

    submit();

    expect(
      await screen.findByText(
        "Call uploaded: the audio is stored and the transcript's 2 lines are attached, each with its time.",
        {},
        WAIT,
      ),
    ).toBeInTheDocument();
    const call = await linkedCall();
    expect(call.session_key).toMatch(/^upload-/);
    expect(call.recordings).toEqual([
      expect.objectContaining({
        state: "stored",
        content_type: "audio/ogg",
        channel_speakers: ["user", "agent"],
      }),
    ]);
    // The transcript span and its two line spans.
    expect(call.span_count).toBe(3);
  });

  it("keeps the first transcript of a recording and says why another is refused", async () => {
    render(<UploadCall />);
    choose("Call audio", monoWav());
    setText("Session key (optional)", "host-call-409");
    choose(
      "Transcript (optional)",
      platformFile(
        "user: I'd like a table.\nagent: For how many?\n",
        "a.txt",
        "text/plain",
      ),
    );
    submit();
    expect(
      await screen.findByText(
        "Call uploaded: the audio is stored and the transcript's 2 lines are attached in order, without times.",
        {},
        WAIT,
      ),
    ).toBeInTheDocument();

    // A corrected transcript for the same upload: the audio is not sent again.
    choose(
      "Transcript (optional)",
      platformFile(
        "user: I'd like a table.\nagent: For how many guests?\n",
        "b.txt",
        "text/plain",
      ),
    );
    submit();

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(
      "The audio is uploaded, but this recording already has a different transcript. Earshot keeps the first transcript and never replaces it.",
    );
    const call = await callBySessionKey("host-call-409");
    expect(call.recordings).toHaveLength(1);
    expect(call.span_count).toBe(3);
  });

  it("explains a transcript it cannot read by its line, then takes the fixed one", async () => {
    render(<UploadCall />);
    choose("Call audio", platformFile(readFileSync(STEREO_OGG), "call.ogg", "audio/ogg"));
    fireEvent.change(screen.getByRole("combobox", { name: "Channels" }), {
      target: { value: "agent-user" },
    });
    setText("Session key (optional)", "host-call-invalid");
    choose(
      "Transcript (optional)",
      platformFile(
        "WEBVTT\n\n00:01.000 --> 00:02.000\nNo speaker.\n",
        "bad.vtt",
        "text/vtt",
      ),
    );
    submit();

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(
      "The audio is uploaded, but the transcript was not attached: line 3: name the speaker with <v user> or <v agent>",
    );

    choose("Transcript (optional)", platformFile(VTT, "good.vtt", "text/vtt"));
    submit();
    expect(
      await screen.findByRole("link", { name: "Open the call" }, WAIT),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    const call = await callBySessionKey("host-call-invalid");
    expect(call.recordings).toEqual([
      expect.objectContaining({ state: "stored", channel_speakers: ["agent", "user"] }),
    ]);
    expect(call.span_count).toBe(3);
  });

  it("says when the file's channels are not the layout chosen", async () => {
    render(<UploadCall />);
    choose("Call audio", monoWav());
    fireEvent.change(screen.getByRole("combobox", { name: "Channels" }), {
      target: { value: "user-agent" },
    });
    submit();

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(
      "The audio could not be uploaded: the file has 1 channel(s); the recording has 2",
    );
  });

  it("says when the project's preset keeps no transcript, or no recording", async () => {
    render(<UploadCall />);
    choose("Call audio", monoWav());
    setText("Session key (optional)", "host-call-preset");
    submit();
    expect(
      await screen.findByText("Call uploaded: the audio is stored.", {}, WAIT),
    ).toBeInTheDocument();

    await setPreset("metadata_only");
    try {
      choose(
        "Transcript (optional)",
        platformFile("user: hello\n", "t.txt", "text/plain"),
      );
      submit();
      expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(
        "The audio is uploaded, but this project's capture preset (metadata_only) keeps no transcript text, so the transcript was not attached.",
      );

      // Another call: the recording itself is refused.
      setText("Session key (optional)", "host-call-preset-2");
      submit();
      expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(
        "This project's capture preset (metadata_only) stores no recordings, so the call was not uploaded.",
      );
    } finally {
      await setPreset("full");
    }
    expect((await callBySessionKey("host-call-preset")).span_count).toBe(0);
  });
});
