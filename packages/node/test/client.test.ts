/**
 * The recordings and calls client against a real `earshot serve` (test/server.ts),
 * with a real project API key and the real LiveKit 1.8 stereo Ogg Opus recording
 * from fixtures/captured.
 */
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, inject, it } from "vitest";
import { EarshotClient, EarshotError, recordingIdFor } from "../src/index.js";

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
