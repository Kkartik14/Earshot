/** Exercise the client against a real server and recording, with local HTTP servers for redirect and proxy failures. */
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";
import { describe, expect, inject, it } from "vitest";
import {
  EarshotClient,
  EarshotError,
  type Recording,
  recordingIdFor,
} from "../src/index.js";

const { baseUrl, apiKey } = inject("earshot");
const RECORDING = fileURLToPath(
  new URL(
    "../../../fixtures/captured/livekit-otlp-1.8/audio/recording.ogg",
    import.meta.url,
  ),
);
const STARTED_AT = 1_792_407_986_846_424_000n;

describe("EarshotClient", () => {
  it("creates a stereo recording, streams it from disk, and finds it through its call", async () => {
    const bytes = await readFile(RECORDING);
    const sessionKey = "node-client-stereo";
    const recordingId = recordingIdFor(sessionKey, "call");
    const earshot = new EarshotClient({ baseUrl: `${baseUrl}/`, apiKey });
    const manifest = {
      session_key: sessionKey,
      speaker: "mixed",
      content_type: "audio/ogg",
      channel_speakers: ["user", "agent"],
      recording_id: recordingId,
    } as const;

    const created = await earshot.createRecording({
      ...manifest,
      started_at_unix_nano: STARTED_AT,
    });
    const retried = await earshot.createRecording({
      ...manifest,
      started_at_unix_nano: String(STARTED_AT),
    });
    const uploaded = await earshot.uploadRecording(created, RECORDING);
    const again = await earshot.uploadRecording(recordingId, new Uint8Array(bytes));
    const { call_id } = await earshot.resolveCall(sessionKey);
    const call = await earshot.getCall(call_id);

    expect([
      created.state,
      created.channel_speakers,
      created.started_at_unix_nano,
    ]).toEqual(["pending", ["user", "agent"], String(STARTED_AT)]);
    expect(retried).toEqual(created);
    expect(uploaded).toMatchObject({
      state: "stored",
      sha256: createHash("sha256").update(bytes).digest("hex"),
      size: bytes.length,
      duration_ms: 34_010,
    });
    expect(again).toEqual(uploaded);
    expect(await earshot.getRecording(recordingId)).toEqual(uploaded);
    expect(call_id).toBe(created.call_id);
    expect(call.call_id).toBe(call_id);
    expect(
      call.recordings.map((item) => [item.recording_id, item.channel_speakers]),
    ).toEqual([[recordingId, ["user", "agent"]]]);
  });

  it("reads the server and key from the OTLP exporter's environment", async () => {
    const earshot = EarshotClient.fromEnvironment({
      OTEL_EXPORTER_OTLP_ENDPOINT: baseUrl,
      OTEL_EXPORTER_OTLP_HEADERS: `x-other=1,Authorization=${encodeURIComponent(`Bearer ${apiKey}`)}`,
    });

    const created = await earshot.createRecording({
      session_key: "node-client-env",
      speaker: "user",
      content_type: "audio/wav",
      started_at_unix_nano: "1792407986846424000",
    });

    expect(created.state).toBe("pending");
    expect(() => EarshotClient.fromEnvironment({})).toThrow(/EARSHOT_URL/);
  });

  it("raises EarshotError with the status and Earshot's code, and never the key", async () => {
    const refusal = (promise: Promise<unknown>) =>
      promise.then(
        () => expect.fail("the request succeeded"),
        (error: unknown) => {
          expect(error).toBeInstanceOf(EarshotError);
          return error as EarshotError;
        },
      );

    const unknown = await refusal(
      new EarshotClient({ baseUrl, apiKey }).getCall(`call_${"0".repeat(32)}`),
    );
    const wrongKey = await refusal(
      new EarshotClient({ baseUrl, apiKey: `${apiKey}x` }).resolveCall("node-client-any"),
    );
    // Nothing listens on port 1: the connection is refused.
    const noAnswer = await refusal(
      new EarshotClient({ baseUrl: "http://127.0.0.1:1", apiKey }).getCall("call_x"),
    );

    expect([unknown.status, unknown.code]).toEqual([404, "EARSHOT_CALL_NOT_FOUND"]);
    expect(wrongKey.status).toBe(401);
    expect(wrongKey.code).toMatch(/^EARSHOT_/);
    expect([noAnswer.status, noAnswer.code]).toEqual([0, null]);
    for (const error of [unknown, wrongKey, noAnswer]) {
      expect(`${error.message} ${error.detail} ${error.stack}`).not.toContain(apiKey);
    }
  });
});

type Seen = { path: string; authorization: string | null };

/** A real local HTTP server that records each request and answers it with `answer`. */
async function answering(
  answer: (request: IncomingMessage, response: ServerResponse) => void,
): Promise<{ url: string; seen: Seen[]; close: () => Promise<void> }> {
  const seen: Seen[] = [];
  const server: Server = createServer((request, response) => {
    seen.push({
      path: request.url ?? "",
      authorization: request.headers.authorization ?? null,
    });
    request.resume();
    request.on("end", () => answer(request, response));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    seen,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}

const json = (response: ServerResponse, body: string) => {
  response.setHeader("Content-Type", "application/json");
  response.end(body);
};

describe("answers an Earshot server never gives", () => {
  it.each([
    [301, "another origin"],
    [302, "another origin"],
    [303, "another origin"],
    [307, "another origin"],
    [308, "another origin"],
    [307, "the same origin"],
    [308, "the same origin"],
  ])(
    "a %i redirect to %s is an EarshotError, and the key never follows it",
    async (status, where) => {
      const key = "earshot_sk_redirect-canary";
      const other = await answering((_, response) =>
        json(response, '{"call_id":"call_x"}'),
      );
      const server = await answering((request, response) => {
        if (request.url?.startsWith("/moved"))
          return json(response, '{"call_id":"call_x"}');
        const origin = where === "another origin" ? other.url : "";
        response.writeHead(status, { Location: `${origin}/moved${request.url}` }).end();
      });
      const earshot = new EarshotClient({ baseUrl: server.url, apiKey: key });
      try {
        const read = await earshot.getCall("call_1").then(
          () => expect.fail("the redirect was followed"),
          (error: unknown) => error,
        );
        const upload = await earshot
          .uploadRecording(
            {
              upload_path: "/v1/recordings/r1/content",
              content_type: "audio/wav",
            } as Recording,
            new Uint8Array(68),
          )
          .then(
            () => expect.fail("the redirect was followed"),
            (error: unknown) => error,
          );

        for (const error of [read, upload]) {
          expect(error).toBeInstanceOf(EarshotError);
          expect([(error as EarshotError).status, (error as EarshotError).code]).toEqual([
            status,
            null,
          ]);
        }
        const followed = [...server.seen, ...other.seen].filter((seen) =>
          seen.path.startsWith("/moved"),
        );
        expect(followed).toEqual([]);
      } finally {
        await Promise.all([server.close(), other.close()]);
      }
    },
  );

  it("a success whose answer is not JSON is an EarshotError", async () => {
    const proxy = await answering((_, response) => {
      response.setHeader("Content-Type", "text/html");
      response.end("<html>a proxy's sign-in page</html>");
    });
    try {
      const error = await new EarshotClient({
        baseUrl: proxy.url,
        apiKey: "earshot_sk_x",
      })
        .getCall("call_1")
        .then(
          () => expect.fail("a page was read as JSON"),
          (error: unknown) => error,
        );

      expect(error).toBeInstanceOf(EarshotError);
      expect([(error as EarshotError).status, (error as EarshotError).code]).toEqual([
        200,
        null,
      ]);
    } finally {
      await proxy.close();
    }
  });
});

describe("recordingIdFor", () => {
  // Vectors from the Python client's recording_id_for, so a Node and a Python
  // process name the same recording for one call.
  it.each([
    ["conv-1", "call", "conv-1.call"],
    ["room/abc 123", "call", "room-abc-123-3948012130b706ff.call"],
    ["x".repeat(200), "call", `${"x".repeat(106)}-aa20c23e32018340.call`],
    ["..lead", "user", "lead-a7da10245d24e49c.user"],
    ["café-😀-ok", "call", "caf----ok-c08c63d1498cf6ef.call"],
  ])("names %s's %s recording %s", (sessionKey, name, expected) => {
    expect(recordingIdFor(sessionKey, name)).toBe(expected);
  });
});
