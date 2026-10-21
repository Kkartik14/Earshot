# @earshot/node

Earshot for Node voice agents. A Node voice server, such as a Twilio Media Streams
handler or a LiveKit Agents JS worker, uses it to:

- **key its spans to a call.** `EarshotSpanProcessor` stamps `earshot.session.id` on
  every span, so the spans your OpenTelemetry exporter sends to Earshot land in the
  right call.
- **upload the call's recording.** The recording is created when the call's audio
  starts, which holds the call open, and its bytes are streamed when the call ends.
- **read the call back.** It resolves a session key to its call and fetches the call's
  detail.

The package is ESM-only and needs Node 22.12 or later. Its one runtime peer is
`@opentelemetry/api`. It is about 300 lines, with types generated from Earshot's
OpenAPI spec.

```sh
npm install @earshot/node @opentelemetry/api @opentelemetry/sdk-trace-node \
  @opentelemetry/exporter-trace-otlp-proto
```

## Send a call's spans

Point the standard OTLP exporter at your Earshot server, then add the processor beside
the exporter's processor:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<project API key>"
```

```ts
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BatchSpanProcessor, NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { EarshotSpanProcessor, withSessionKey } from "@earshot/node";

const provider = new NodeTracerProvider({
  spanProcessors: [
    new EarshotSpanProcessor(),
    new BatchSpanProcessor(new OTLPTraceExporter()),
  ],
});
provider.register(); // installs the async context manager withSessionKey relies on

await withSessionKey(callSid, async () => {
  // Every span started in here, in any async continuation, carries callSid.
});
```

The processor takes the key from these sources, highest priority first:

1. **The span's own `earshot.session.id`.** A span started with one keeps it.
2. **The key in the span's context.** Set it with `withSessionKey(key, fn)`, or with
   `setSessionKey(context, key)` when you pass contexts yourself. The key stays
   in-process: it is not OpenTelemetry baggage, so it is never propagated to other
   services.
3. **A fixed key**, from `new EarshotSpanProcessor({ sessionKey })`, for a process that
   serves a single call.

A span with no key from any of these is exported as is.

A key must be 1 to 256 bytes of UTF-8 after trimming, the rule the server applies. Any
other key throws a `RangeError` where you set it, instead of silently leaving the call
without spans.

LiveKit Agents JS (checked at 1.9.3) already stamps `gen_ai.conversation.id` (the room
SID) on every span, and Earshot reads it too, so a LiveKit worker needs the processor
only to choose its own key.

## Upload the recording and read the call

```ts
import { EarshotClient, recordingIdFor } from "@earshot/node";

const earshot = EarshotClient.fromEnvironment();

// At the call's first audio. The pending recording holds the call open until it is uploaded.
const recording = await earshot.createRecording({
  session_key: callSid,
  recording_id: recordingIdFor(callSid, "call"), // a retry names the same recording
  speaker: "mixed",
  channel_speakers: ["user", "agent"], // a stereo file: channel 0 user, channel 1 agent
  content_type: "audio/wav",
  started_at_unix_nano: BigInt(Date.now()) * 1_000_000n, // wall clock of the first sample
});

// After the call: streamed from disk with its sha-256 Content-Digest.
await earshot.uploadRecording(recording, "/tmp/call.wav");

const { call_id } = await earshot.resolveCall(callSid);
const call = await earshot.getCall(call_id);
```

- **`EarshotClient.fromEnvironment()`** finds the server and the key:
  - the server is `EARSHOT_URL`, else `OTEL_EXPORTER_OTLP_ENDPOINT`;
  - the key is `EARSHOT_API_KEY`, else the `Authorization` entry of
    `OTEL_EXPORTER_OTLP_HEADERS`, the one the exporter already sends.

  Alternatively, construct a client with `new EarshotClient({ baseUrl, apiKey })`.

- **`uploadRecording`** takes the recording that `createRecording` returned, or the
  recording's ID. The source is a file path, which is streamed and never held whole in
  memory, or a `Uint8Array`. Uploading the same bytes again returns the stored
  recording.
- **Errors.** A refused request throws `EarshotError`, which carries the HTTP `status`
  and Earshot's stable `code`, for example `EARSHOT_CALL_NOT_FOUND`. A request that gets
  no answer has `status` 0. Error messages never contain the API key.
- **Timeouts.** `timeoutMs` bounds each request, uploads included. Without it, a stalled
  request is ended by Node's fetch idle timeouts.
- **Types.** `Recording`, `CallDetail` and `CallResolution` are the server's response
  types. `paths` and `components` cover Earshot's whole HTTP API. Use them with a typed
  fetch client such as `openapi-fetch` for the routes this package doesn't wrap, such as
  the calls list.

`recordingIdFor(sessionKey, name)` gives the same ID as the Python client's
`earshot.client.recording_id_for`, so a Node process and a Python process name the same
recording.

## License

Apache-2.0
