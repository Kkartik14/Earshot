# OTLP quick start

Earshot receives OpenTelemetry traces at `POST /v1/traces` and groups them into Calls.
A Call is one voice session: every span that shares a Session Key in a Project, plus the
Recordings uploaded for it. When a Call closes, Earshot stores a Revision, an ordinary
Incident built from all of its spans and Recordings.

This page covers Pipecat 1.5 and LiveKit Agents 1.6 to 1.8, the producers tested end to
end in 0.3.0, and the two clients that upload a call's recording: Python's
[`earshot.client`](#the-python-client-earshotclient) and Node's
[`@earshot/node`](#node-earshotnode). Any OTLP/HTTP producer can send spans; see the
[README](../README.md#what-earshot-receives-today) for what each source gets. Once a Call
has closed, its [call view](./calls.md) shows its audio, transcript, latency, audible gap
and cost.

Everything on this page about `/v1/traces`, `/v1/recordings*`, `/v1/calls*` and
`/v1/otlp/stats` describes the 0.3.0 receiver, recordings and call APIs.

## Start the server

```bash
pip install 'earshot-observability[server,signals]'
earshot serve
```

The default listener is `http://127.0.0.1:4319`, with SQLite and local disk under
`./.earshot`. [Storage and blobs](./storage-and-blobs.md) covers Postgres and S3. The
`signals` extra (numpy, onnxruntime, soundfile and a vendored voice activity model)
analyses each Recording for the call view's audible gap and waveform; without it the
server runs, and every analysis is `unavailable` with `EARSHOT_SIGNALS_EXTRA_REQUIRED`.

## Use the HTTP exporter, not gRPC

Earshot implements OTLP over HTTP only. There is no gRPC listener in 0.3.0. Pipecat's own
tracing examples use the gRPC exporter (`opentelemetry-exporter-otlp-proto-grpc`); switch
to the HTTP one:

```bash
pip install 'pipecat-ai[tracing]>=1.5,<1.6' opentelemetry-exporter-otlp-proto-http
```

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
```

The HTTP exporter sends protobuf. Earshot also accepts OTLP/JSON
(`Content-Type: application/json`) from other clients.

## Environment variables

The exporter reads the standard OpenTelemetry variables. Earshot needs no extra client
library.

| Variable                             | Value                                                                                                                                           |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `OTEL_EXPORTER_OTLP_ENDPOINT`        | `http://127.0.0.1:4319`. The exporter appends `/v1/traces`. Earshot uses its API port, 4319, not the OTLP default 4318, so set this explicitly. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Optional. Used as is, so it must end in `/v1/traces`.                                                                                           |
| `OTEL_EXPORTER_OTLP_HEADERS`         | `Authorization=Bearer%20<api-key>`. Needed off loopback; see below.                                                                             |
| `OTEL_EXPORTER_OTLP_COMPRESSION`     | Optional. Earshot accepts `gzip` and `deflate`.                                                                                                 |
| `OTEL_EXPORTER_OTLP_TIMEOUT`         | Optional. The Python exporter gives up on a batch after this long, 10 s by default.                                                             |

The header value is percent-encoded, so `%20` is the space after `Bearer`. The Python
exporter also accepts a literal space.

## API keys and the host rule

On a loopback listener with no `EARSHOT_TOKEN`, requests need no credential and land in
the `default` Project. Anywhere else, send a Project API key. A key also chooses the
Project the spans belong to.

```bash
earshot project create support --display-name "Support voice agent"
earshot api-key issue --project support --label pipecat
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<credential>"
```

`api-key issue` prints the credential once. Earshot stores only its hash.

The server also checks the `Host` header, to stop DNS rebinding against a server that
answers without a credential. The check applies only where a request can be anonymous:
under `EARSHOT_TRUST_LOCAL_NETWORK`, or on a loopback listener with neither
`EARSHOT_TOKEN` nor `EARSHOT_BEHIND_TLS_PROXY`. Under `EARSHOT_TRUST_LOCAL_NETWORK`, a
request that carries a valid API key or the `EARSHOT_TOKEN` operator token is exempt,
because a rebinding page cannot send a credential it does not have. This is what lets a
bot in another container post to `http://earshot:4319`. Anonymous requests, and requests
with an invalid key or token, keep the check.

The full rule, for a server in operator mode. "Valid key" is an API key of the Project
`support`; "accepted" names the Project the spans are stored in. The listener address
is in parentheses.

| Setting                                                                  | Credential           | `Host: 127.0.0.1:4319`     | `Host: earshot:4319`         |
| ------------------------------------------------------------------------ | -------------------- | -------------------------- | ---------------------------- |
| `EARSHOT_TRUST_LOCAL_NETWORK` (`0.0.0.0`)                                | none                 | accepted, `default`        | 400 `EARSHOT_UNTRUSTED_HOST` |
| `EARSHOT_TRUST_LOCAL_NETWORK` (`0.0.0.0`)                                | valid key            | accepted, `support`        | accepted, `support`          |
| `EARSHOT_TRUST_LOCAL_NETWORK` (`0.0.0.0`)                                | invalid key          | 401 `EARSHOT_UNAUTHORIZED` | 400 `EARSHOT_UNTRUSTED_HOST` |
| `EARSHOT_TRUST_LOCAL_NETWORK` and `EARSHOT_TOKEN` (`0.0.0.0`)            | none                 | 401 `EARSHOT_UNAUTHORIZED` | 400 `EARSHOT_UNTRUSTED_HOST` |
| `EARSHOT_TRUST_LOCAL_NETWORK` and `EARSHOT_TOKEN` (`0.0.0.0`)            | valid key            | accepted, `support`        | accepted, `support`          |
| `EARSHOT_TRUST_LOCAL_NETWORK` and `EARSHOT_TOKEN` (`0.0.0.0`)            | operator token       | accepted, `default`        | accepted, `default`          |
| `EARSHOT_TRUST_LOCAL_NETWORK` and `EARSHOT_TOKEN` (`0.0.0.0`)            | invalid key or token | 401 `EARSHOT_UNAUTHORIZED` | 400 `EARSHOT_UNTRUSTED_HOST` |
| `EARSHOT_BEHIND_TLS_PROXY` (any), or `EARSHOT_TOKEN` alone (`127.0.0.1`) | none                 | 401 `EARSHOT_UNAUTHORIZED` | 401 `EARSHOT_UNAUTHORIZED`   |
| `EARSHOT_BEHIND_TLS_PROXY` (any), or `EARSHOT_TOKEN` alone (`127.0.0.1`) | valid key            | accepted, `support`        | accepted, `support`          |
| `EARSHOT_BEHIND_TLS_PROXY` (any), or `EARSHOT_TOKEN` alone (`127.0.0.1`) | operator token       | accepted, `default`        | accepted, `default`          |
| `EARSHOT_BEHIND_TLS_PROXY` (any), or `EARSHOT_TOKEN` alone (`127.0.0.1`) | invalid key or token | 401 `EARSHOT_UNAUTHORIZED` | 401 `EARSHOT_UNAUTHORIZED`   |
| neither, and no `EARSHOT_TOKEN` (`127.0.0.1`)                            | none                 | accepted, `default`        | 400 `EARSHOT_UNTRUSTED_HOST` |
| neither, and no `EARSHOT_TOKEN` (`127.0.0.1`)                            | valid key            | accepted, `support`        | 400 `EARSHOT_UNTRUSTED_HOST` |
| neither, and no `EARSHOT_TOKEN` (`127.0.0.1`)                            | invalid key          | 401 `EARSHOT_UNAUTHORIZED` | 400 `EARSHOT_UNTRUSTED_HOST` |

The operator-token rows apply only when `EARSHOT_TOKEN` is set. Behind a TLS proxy every
request needs a credential, so there is no Host check and the proxy can forward the
public `Host`, whatever the listener address. With both `EARSHOT_TRUST_LOCAL_NETWORK`
and `EARSHOT_BEHIND_TLS_PROXY` set, the `EARSHOT_TRUST_LOCAL_NETWORK` rows apply.

The rule and its exemption are the same on every `/v1` route, not only `/v1/traces`.
Connector webhooks under `/hooks/v1/` keep the check without the exemption. In
`hosted_jwt` mode there is no Host check, and `/v1/traces`, `/v1/recordings*`,
`/v1/calls*` and `/v1/otlp/stats` answer 403 `EARSHOT_OPERATION_UNAVAILABLE` for every
method; the hosted `/openapi.json` does not list them.

## Session keys

Earshot reads the Session Key from the first of these span attributes that holds a valid
key:

1. `earshot.session.id`
2. `gen_ai.conversation.id`
3. `session.id`
4. `conversation.id`

On the resource, only `earshot.session.id` is read. A valid key is a UTF-8 string of 1 to
256 bytes after trimming; anything else counts as no key. The Call records which attribute
won as its `key_source`.

Set `earshot.session.id` to your own call ID; it is the key the Recordings API takes.
The order above applies within one span: on a span that carries several of these
attributes, `earshot.session.id` wins. It does not override a key the trace already has.
A trace keeps the first key Earshot sees in it (below), so `earshot.session.id` decides
the Call only when it is on the first keyed span Earshot receives for that trace.
`session.id` is ranked low on purpose: OpenTelemetry defines it as a period of user
activity that can hold several calls, and two calls keyed only by one `session.id` are
merged into one Call.

**Traces bind to Calls.** A trace joins the Call of the first keyed span Earshot sees in
it, and the first binding wins. Unkeyed spans in that trace join the same Call. Pipecat
relies on this: its `stt`, `llm` and `tts` spans carry no key, but they share a trace with
the `turn` spans, which carry `conversation.id`, and with the `conversation` span, which
carries `conversation.id` and the attributes you pass in `additional_span_attributes`. A
span in the same trace that names a different key still joins the bound Call, and is
counted as a `key_disagreements` event.

**Pipecat 1.5: use one value for both keys.** Pass the same ID as `conversation_id` and
as `earshot.session.id`, as the [snippet](#pipecat-snippet) does. This is a known
limitation of 0.3.0. Pipecat 1.5 puts `conversation.id` on every `turn` span, but puts
`additional_span_attributes` only on the `conversation` span, which it exports when the
call ends. The `turn` spans arrive first, so the trace is already bound to the
`conversation.id` key when `earshot.session.id` arrives. If the two values differ:

- the Call is keyed by the `conversation_id` value (`key_source: conversation.id`), and a
  `key_disagreements` event is counted;
- `POST /v1/calls:resolve` with the `earshot.session.id` value answers 404;
- a Recording created with the `earshot.session.id` value forms a second Call.

If you leave out `conversation_id`, Pipecat generates a random one, and the Call is keyed
by it.

**Unkeyed traces are parked.** Spans of a trace with no key yet are held. A keyed span of
that trace, in any later request, binds it, and the parked spans join the Call. Five
minutes (`EARSHOT_CALL_LONG_QUIET_SECONDS`) after a parked trace's last span:

- if it holds a framework session span (Pipecat's `conversation`, LiveKit's
  `agent_session`), it becomes a Call keyed by its trace ID (`key_source: trace_id`);
- otherwise its spans are deleted and counted as `parked_traces_discarded`.

To find a Call from your own key, `POST /v1/calls:resolve` with
`{"session_key": "call-123"}`. It returns `{"call_id": "call_…"}` or 404
`EARSHOT_CALL_NOT_FOUND`, and never echoes the key. After a purge it finds only a new
Call for the key, never the purged one.

## End markers and close timing

A Call is `open` while evidence arrives. It closes after an end marker or a quiet period.
End markers are matched by instrumentation scope and span name, never by span status:

| Producer      | End marker                                              |
| ------------- | ------------------------------------------------------- |
| Pipecat       | scope `pipecat.turn`, span `conversation`               |
| LiveKit ≤ 1.7 | scope `livekit-agents`, span `agent_session`            |
| LiveKit 1.8   | scope `livekit-agents`, span `session_close`            |
| Any producer  | any span with the attribute `earshot.call.ended = true` |

The close time is recomputed on every evidence write, a new span or a stored Recording.
An identical re-sent span is not evidence and moves nothing.

LiveKit 1.8 sends its job's shutdown spans after `session_close`: `agent_session`,
`on_session_end`, `session_end_upload`, `room_disconnect`, `job_shutdown`,
`job_entrypoint` and, if the host registers one, `shutdown_callback`. They are stored
with the Call, but never reopen it, move its close time or count as its latest
evidence. On LiveKit 1.6 and 1.7, which send no `session_close`, `agent_session` is the
end marker.

| Variable                                | Default | Rule                                                                                                                                                           |
| --------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EARSHOT_CALL_END_GRACE_SECONDS`        | `10`    | After an end marker, the Call closes this long after its latest evidence.                                                                                      |
| `EARSHOT_CALL_LONG_QUIET_SECONDS`       | `300`   | Without an end marker, the Call closes after this long with no new evidence.                                                                                   |
| `EARSHOT_CALL_LATE_DEBOUNCE_SECONDS`    | `30`    | A closed Call that receives late evidence is revised no sooner than this after it, and no sooner than the two rules above allow.                               |
| `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` | `600`   | A `pending` Recording keeps its Call from closing until this long after the latest of the Recording's creation, the Call's latest evidence and its end marker. |

The server looks for due Calls every second. Each close stores a Revision with bundle ID
`<call_id>.r<n>`. Its `close_reason` is `end_signal` for the first close after the end
marker, `late_data` for a later close of a Call that already has a Revision, and
`quiet_period` for a first close without an end marker. Once the end marker has arrived,
every Revision is `final`; a close before it is partial, with status `interrupted`. Late
spans or Recordings make the Call dirty again and produce the next Revision.

Two consequences:

- Call `force_flush()` on the tracer provider before the process exits. Pipecat exports
  the `conversation` span when the call ends; if that batch is lost, the Call closes only
  after the quiet period, as `interrupted`.
- A crashed bot never sends its end marker, so its Call closes 5 minutes after the last
  span.

A Call stores at most 16 Revisions, plus one `final` Revision if its end marker arrives
after the sixteenth. Past the cap, evidence is still stored but no Revision shows it: the
Call reports `evidence_after_latest_revision: true`, and the `revision_cap_reached`
counter goes up once. A Call that holds no evidence when it closes (its only Recording
never uploaded) closes without a Revision. If a Revision cannot be built, the Call reports
the code in `materialize_error` and is retried after 2 s, then 4 s, 8 s and so on, never
waiting longer than `EARSHOT_CALL_LONG_QUIET_SECONDS`.

**What a Revision holds.** A Revision is built from the Call's Pipecat or LiveKit spans
and its stored Recordings. Spans from other instrumentation stay stored and are declared
as omitted. An LLM request's input (Pipecat's `llm` `input`, LiveKit's `lk.chat_ctx` or
`gen_ai.input.messages`) repeats the whole conversation on every turn, so it is always
left out of the Revision, with a declared omission; the Revision then grows with the
length of the call, not its square, and the full spans stay stored. Each Revision
covers the whole Call, so `GET /v1/metrics/turns` and Turn Fact queries count only a
Call's latest Revision. Earlier Revisions stay readable.

## Recordings

A Recording is one speaker's mono track, or a `mixed` stereo file whose
`channel_speakers` names each channel's speaker: `["user", "agent"]` puts the user on
channel 0 and the agent on channel 1. A stereo file keeps both speakers on one clock, so
prefer it when your framework records both. Earshot reads the header of a WAV, FLAC or
Ogg Opus upload as it streams, and refuses a file whose channel count is not the
Recording's (one, or one per `channel_speakers` entry) with 422
`EARSHOT_RECORDING_CHANNEL_MISMATCH`, and a file it cannot read with 422
`EARSHOT_RECORDING_INVALID_WAV`, `EARSHOT_RECORDING_INVALID_FLAC` or
`EARSHOT_RECORDING_INVALID_OGG` (Ogg holding another codec than Opus included). It does
not inspect MP3 or WebM, so send those mono, without `channel_speakers`. The Project's
Capture Preset must be `full`; under `metadata_only`, creating or uploading a Recording
gives 422 `EARSHOT_PRESET_FORBIDS_MEDIA`.

An upload has two steps: `POST` creates the Recording, then `PUT` uploads its bytes.

**1. Create it.** `POST /v1/recordings` with a JSON body:

| Field                  | Required | Value                                                                                                                                                                              |
| ---------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_key`          | yes      | The same key as `earshot.session.id`. It finds the Call, or creates it.                                                                                                            |
| `speaker`              | yes      | `user`, `agent` or `mixed`.                                                                                                                                                        |
| `content_type`         | yes      | `audio/wav`, `audio/ogg`, `audio/mpeg`, `audio/webm` or `audio/flac`.                                                                                                              |
| `started_at_unix_nano` | yes      | Wall-clock time of the first sample, as a decimal string from 1 to 2^63 − 1 (as span times). It aligns the audio with the spans; see below.                                        |
| `recording_id`         | no       | `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. Makes the request idempotent.                                                                                                                |
| `label`                | no       | Up to 128 characters.                                                                                                                                                              |
| `channel_speakers`     | no       | With `speaker: "mixed"` only: each channel's speaker in order, `user` and `agent` at most once each. WAV, FLAC or Ogg Opus only; otherwise 422 `EARSHOT_INVALID_CHANNEL_SPEAKERS`. |

The answer is 201 with `recording_id`, `call_id`, `state: "pending"` and a relative
`upload_path` (`/v1/recordings/<id>/content`). Sending the same `recording_id` again with
the same body returns 200 and the existing Recording; with a different body, or for a
`failed` Recording, it returns 409 `EARSHOT_RECORDING_CONFLICT`. If the Session Key's
Call was purged and its tombstone is still live, it returns 409 `EARSHOT_CALL_PURGED` and
counts a `purged_call_evidence_dropped` event. Without a `recording_id` the server mints
one, and a retry creates a second Recording.

A Call holds at most 16 Recordings, in any state: a Revision gives each its own media
timeline, and holds at most 16. Creating a 17th returns 409 `EARSHOT_RECORDING_LIMIT`,
and stores nothing; sending an existing `recording_id` again is still answered as above.

**Time zero.** Stamp `started_at_unix_nano` where the first audio frame enters the
pipeline, at the input transport. A later stamp, such as one taken where a recorder
first sees audio, is off by however long the frames took to get there, and every
Recording is then misaligned with the spans by that much. The example's `WavCallerInput`
(`examples/pipecat_otlp/call_io.py`) stamps it when it produces its first frame, which
is sample 0 of both tracks.

Create the Recordings when the call starts, as the example does, and upload them after
it ends. A `pending` Recording keeps its Call from closing until 10 minutes after the
latest of its creation, the Call's latest evidence and the Call's end marker, however
long the call runs, so the first Revision includes the audio. A Recording created only
after the Call has closed misses that Revision: its audio goes into the next one, a
`late_data` Revision made about 30 seconds (`EARSHOT_CALL_LATE_DEBOUNCE_SECONDS`) after
the upload.

**2. Upload it.** `PUT /v1/recordings/<id>/content` with the raw bytes as the body:

- `Content-Type` must equal the created `content_type`, or 415.
- The body may be up to `EARSHOT_RECORDING_MAX_BYTES` (512 MiB by default), or 413.
- An optional `Content-Digest: sha-256=:<base64>:` header is checked while streaming. A
  mismatch gives 400 and nothing is stored.
- On success the Recording is `stored`, with its sha256, size and, for WAV, FLAC and Ogg
  Opus, its duration.
- A repeated `PUT` to a `stored` Recording is hashed without being written: the same bytes
  give 200, different bytes give 409. A `PUT` to a `failed` Recording gives 409.

A Recording still `pending` past that deadline (`EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS`
after the latest of the three times above) becomes `failed`, and any partial upload is
deleted. A `PUT` that is still sending bytes is not cut off this way: while it streams,
the Recording stays `pending` and keeps holding its Call, so the Call's first Revision
still includes the audio. Two bounds end a `PUT`, so that no client can hold its Call
open for good:

- one that sends no bytes for 60 seconds is ended with 408 `EARSHOT_UPLOAD_IDLE_TIMEOUT`
  (a client whose connection died);
- one still sending `EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS` (10 minutes by default) after
  it started is ended with 408 `EARSHOT_UPLOAD_DEADLINE_EXCEEDED`, however steadily it
  sends. Raise that setting if a Recording can take longer than that to upload.

The deadline applies again once the `PUT` ends without storing; retry the upload with a
new `PUT` before it passes.

**Read it back.** `GET /v1/recordings/<id>` returns the metadata, and
`GET /v1/recordings/<id>/content` streams the bytes through the server, with `Range`
support (206). A `pending` Recording gives 409 `EARSHOT_RECORDING_NOT_STORED`. There are
no presigned URLs in 0.3.0. Once the server has analysed it,
`GET /v1/recordings/<id>/peaks` serves its waveform peaks; see
[the call view](./calls.md#audio-content-peaks-and-renditions).

**The example's upload flow.** `python examples/pipecat_otlp/run_call.py --upload
[BASE_URL]` runs both steps for a real call with `earshot.client`. At the first audio
frame it creates one `mixed` Recording with `channel_speakers: ["user", "agent"]`, keyed
by the conversation ID, with a recording ID derived from the conversation ID, so a retry
is idempotent. After the call and the tracer's `force_flush`, it uploads one stereo WAV
built from `AudioBufferProcessor`'s two tracks, streamed with a `Content-Digest`.
`BASE_URL` defaults to `OTEL_EXPORTER_OTLP_ENDPOINT`; the API key comes from
`EARSHOT_API_KEY`, or from the `Authorization` entry of `OTEL_EXPORTER_OTLP_HEADERS`.
[`earshot.client`](../packages/sdk-python/src/earshot/client.py) needs only the standard
library (it uses httpx when installed). `examples/livekit_otlp/run_call.py --upload` does
the same for LiveKit's own stereo Ogg Opus recording.

## Responses, errors and retries

| Status        | When                                                                                                                                                                                                                           | Body                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| 200           | The request was stored. Rejected spans, if any, are counted in `partial_success`. An empty request also gets 200.                                                                                                              | `ExportTraceServiceResponse`                            |
| 400           | The body cannot be decoded, including malformed gzip or deflate, and invalid UTF-8 or a non-object in JSON.                                                                                                                    | `google.rpc.Status`                                     |
| 410           | The Project began deletion between authentication and the write. Do not retry.                                                                                                                                                 | `google.rpc.Status`                                     |
| 413           | The body is over `EARSHOT_MAX_BODY_BYTES` (16 MiB) as sent, or over 32 MiB (protobuf) or 4 MiB (JSON) decompressed.                                                                                                            | `google.rpc.Status`                                     |
| 415           | The `Content-Type` is not protobuf or JSON (the Status is then JSON, because there is no request encoding to answer in), or the `Content-Encoding` is not `identity`, `gzip` or `deflate`.                                     | `google.rpc.Status`                                     |
| 503           | Earshot is busy: `EARSHOT_DB_POOL_SIZE` requests are already in flight, its write locks were not free within 2 s, no database connection was free, or the store failed in a way a retry can fix. Always with `Retry-After: 3`. | `google.rpc.Status`                                     |
| 400, 401, 403 | The host, authentication and project checks above.                                                                                                                                                                             | Earshot problem JSON (`{"error": {"code", "message"}}`) |

Handler errors use the request's encoding, except the 415 for an unknown `Content-Type`.
Errors from the shared host and auth checks keep Earshot's problem JSON, because those
checks run before the route is known. Status messages are fixed text and never quote what
the client sent.

**Never 429.** The Python exporter does not retry 429, so the batch would be lost. It
retries 408 and 5xx with backoff, within `OTEL_EXPORTER_OTLP_TIMEOUT`. It ignores
`Retry-After` and never reads `partial_success`.

**Retries are safe.** One request is one transaction, and 200 is sent only after it
commits. Spans are keyed by trace ID and span ID, so an identical re-send stores nothing
new. A different payload for a span Earshot already has keeps the first one and counts a
`span_conflicts` event.

**Rejected spans.** A span is rejected on its own, and the rest of the request is stored,
when its trace ID is not 16 non-zero bytes, its span ID is not 8 non-zero bytes, its parent
or link IDs have the wrong length, its start, end or an event time is outside
[1, 2^63−1], it ends before it starts, its name, scope name or Session Key contains
U+0000, or an attribute key of the span, one of its events or links, its resource or its
scope is longer than 1024 bytes of UTF-8. A JSON ID that is not hex or base64 of the right
length rejects only its span.

What the spans of a request share is bounded too, because Earshot stores it more than
once: a span's row holds its scope name, and a resource is stored again with each scope
under it. Each bound rejects only the spans it applies to:

| Bound                                                    | Limit              | Rejects                                          |
| -------------------------------------------------------- | ------------------ | ------------------------------------------------ |
| Instrumentation scope name                               | 256 bytes of UTF-8 | the spans of that scope                          |
| Scope attributes, serialized                             | 16 KiB             | the spans of that scope                          |
| Resource, serialized, attributes and schema URL included | 64 KiB             | the spans of that resource                       |
| Distinct instrumentation scopes in one request           | 256                | the spans of every later scope, in request order |

Scopes are distinct when their name, version, attributes or schema URL differ. Equal
scopes count once, in any number of `ScopeSpans` and under any resource, and a scope
counts only once one of its spans is accepted. SDK exporters stay far inside these
bounds: one resource per process and a few scopes. A Collector that batches many services
into one request can pass 256 scopes; the later scopes' spans are then rejected and
counted. Keep such batches at or under 256 distinct scopes, for example with a smaller
`send_batch_max_size`.

The `partial_success` error message gives the count per reason, never span values:

```text
2000 spans rejected: 2000 with an instrumentation scope name longer than 256 bytes
```

Because the Python exporter hides per-span rejections, read the counters:

```bash
curl http://127.0.0.1:4319/v1/otlp/stats
```

It returns, for the authenticated Project, the counters `rejected_spans`, `span_conflicts`,
`parked_traces_discarded`, `key_disagreements`, `revision_cap_reached` and
`purged_call_evidence_dropped`, and the gauges `parked_traces` and `open_calls`.

The exporter gives up after its timeout, so a busy or restarting server can lose a batch.
For production, put an OpenTelemetry Collector with a persistent queue in front; see the
[production notes](./production.md#put-a-collector-in-front).

## Pipecat snippet

This follows [`examples/pipecat_otlp/run_call.py`](../examples/pipecat_otlp/run_call.py),
which runs a real call. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and `OTEL_EXPORTER_OTLP_HEADERS`
off loopback) before the process starts. The bot needs only the base install,
`pip install earshot-observability`, for `earshot.client`.

```python
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from pipecat.pipeline.worker import PipelineWorker
from pipecat.utils.tracing.setup import setup_tracing

from earshot.client import EarshotClient, recording_id_for

SESSION_KEY = "call-123"  # your own ID for this call

# The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS.
setup_tracing(service_name="my-voice-agent", exporter=OTLPSpanExporter())

worker = PipelineWorker(
    pipeline,  # your Pipecat pipeline
    enable_tracing=True,
    enable_turn_tracking=True,
    conversation_id=SESSION_KEY,
    additional_span_attributes={"earshot.session.id": SESSION_KEY},
)

# The same server and API key as the exporter.
earshot = EarshotClient.from_environment()

# 1. When the input transport produces its first audio frame (time zero): create one
#    Recording per speaker. They hold the Call open.
started_at = time.time_ns()
recordings = {
    speaker: earshot.create_recording(
        session_key=SESSION_KEY,
        recording_id=recording_id_for(SESSION_KEY, speaker),  # a retry names the same one
        speaker=speaker,
        content_type="audio/wav",
        started_at_unix_nano=started_at,
    )
    for speaker in ("user", "agent")
}

# 2. Run the call. 3. Flush, so the `conversation` span (the end marker) is sent.
trace.get_tracer_provider().force_flush()

# 4. Upload each mono WAV, for example the tracks of Pipecat's AudioBufferProcessor.
for speaker, recording in recordings.items():
    earshot.upload_recording(recording, f"{speaker}.wav")
earshot.close()
```

The two tracks share one `started_at`, and `AudioBufferProcessor` writes them at the same
length, so they are one recorder timeline: the server measures each turn's audible gap
on them and plays them as one stereo rendition. To upload one stereo file instead,
create a single `mixed` Recording with `channel_speakers=["user", "agent"]`, as the
example does.

## LiveKit snippet

This follows [`examples/livekit_otlp/run_call.py`](../examples/livekit_otlp/run_call.py),
which runs a real LiveKit Agents 1.8 call. LiveKit's own `RecorderIO` records the call
as one stereo Ogg Opus file (channel 0 the user, channel 1 the agent), and
`tracer_metadata` stamps the Session Key on every span LiveKit emits.

```python
import asyncio

from livekit.agents import AgentServer, AgentSession, JobContext, telemetry
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from earshot.client import EarshotClient, recording_id_for, tracer_metadata

SESSION_KEY = "call-123"  # your own ID for this call
# Record audio only: nothing is sent to LiveKit's own observability service.
RECORD = {"audio": True, "traces": False, "logs": False, "transcript": False}

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
telemetry.set_tracer_provider(provider, metadata=tracer_metadata(SESSION_KEY))
earshot = EarshotClient.from_environment()
held = {}


async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(stt=..., llm=..., tts=..., vad=...)  # your providers
    await session.start(agent=MyAgent(), room=ctx.room, record=RECORD)
    # The recording's time zero. LiveKit 1.8 makes it public only in the session
    # report, after the call; read it now, so the Recording holds the Call open.
    started_at = session._recorder_io.recording_started_at  # time.time() seconds
    held["recording"] = await asyncio.to_thread(
        earshot.create_recording,
        session_key=SESSION_KEY,
        recording_id=recording_id_for(SESSION_KEY, "call"),
        speaker="mixed",
        channel_speakers=["user", "agent"],
        content_type="audio/ogg",
        started_at_unix_nano=round(started_at * 1e6) * 1000,
    )


async def on_session_end(ctx: JobContext) -> None:
    report = ctx.make_session_report()
    await asyncio.to_thread(
        earshot.upload_recording, held["recording"], report.audio_recording_path
    )


server = AgentServer()
server.rtc_session(entrypoint, on_session_end=on_session_end)
```

Call `provider.force_flush()` before the process exits, so `session_close` (the end
marker) is sent. The example runs headless, with no LiveKit server or room (an
in-process fake job); it proves the path from LiveKit's recorder to the call view, not
the gap a caller in a real room would hear.

## The Python client: `earshot.client`

[`earshot.client`](../packages/sdk-python/src/earshot/client.py) is in the base install
and needs only the standard library; it uses httpx when installed.

| Name                                                                                                                       | Does                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `EarshotClient(base_url, api_key=… or authorization=…, timeout=…)`                                                         | One server, as one Project's API key. Use it as a context manager, or `close()` it.                                                                                                                    |
| `EarshotClient.from_environment()`                                                                                         | The server from `EARSHOT_URL`, else `OTEL_EXPORTER_OTLP_ENDPOINT`; the key from `EARSHOT_API_KEY`, else the `Authorization` entry of `OTEL_EXPORTER_OTLP_HEADERS`, the one the exporter already sends. |
| `create_recording(session_key=, speaker=, content_type=, started_at_unix_nano=, channel_speakers=, recording_id=, label=)` | `POST /v1/recordings`. Idempotent with a `recording_id`.                                                                                                                                               |
| `upload_recording(recording, path_or_file)`                                                                                | `PUT …/content`, streamed in 1 MiB blocks with its `Content-Digest`, never read whole into memory. `recording` is what `create_recording` returned, or its ID.                                         |
| `get_recording(id)`, `resolve_call(session_key)`, `get_call(call_id)`                                                      | The Recording, `{"call_id": …}`, and the call detail with its `projection`.                                                                                                                            |
| `tracer_metadata(session_key)`                                                                                             | `{"earshot.session.id": key}` for LiveKit's `set_tracer_provider(provider, metadata=…)`. Refuses a key the server would not read.                                                                      |
| `recording_id_for(session_key, name)`                                                                                      | A stable Recording ID for a Call's named Recording, so a retry names the same one.                                                                                                                     |
| `EarshotError`                                                                                                             | A refused request: `status` (0 when there was no answer) and Earshot's stable `code`. Its message never holds the API key or a Session Key.                                                            |

## Node: `@earshot/node`

A Node voice server, such as a Twilio Media Streams handler or a LiveKit Agents JS worker,
uses [`@earshot/node`](../packages/node/README.md) (ESM, Node 22.12 or later, with
`@opentelemetry/api` as its one peer):

```ts
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { BatchSpanProcessor, NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  EarshotClient,
  EarshotSpanProcessor,
  recordingIdFor,
  withSessionKey,
} from "@earshot/node";

const provider = new NodeTracerProvider({
  spanProcessors: [
    new EarshotSpanProcessor(),
    new BatchSpanProcessor(new OTLPTraceExporter()),
  ],
});
provider.register(); // installs the async context manager withSessionKey relies on
const earshot = EarshotClient.fromEnvironment();

await withSessionKey(callSid, async () => {
  // Every span started in here, in any async continuation, carries callSid.
  const recording = await earshot.createRecording({
    session_key: callSid,
    recording_id: recordingIdFor(callSid, "call"),
    speaker: "mixed",
    channel_speakers: ["user", "agent"],
    content_type: "audio/wav",
    started_at_unix_nano: BigInt(Date.now()) * 1_000_000n,
  });
  // ... the call runs ...
  await earshot.uploadRecording(recording, "/tmp/call.wav");
});
```

`recordingIdFor` names the same Recording as Python's `recording_id_for`. The package
README covers the processor's key precedence, errors and types.

## Check the result

```bash
curl http://127.0.0.1:4319/v1/calls
curl http://127.0.0.1:4319/v1/calls/<call_id>
```

`GET /v1/calls` lists Calls newest first by first evidence, with a cursor that stays
stable while Calls arrive and receive evidence: `?state=open|closed`, `?limit=` (50 by
default, at most 100) and `?cursor=` (the previous page's `next_cursor`). Each item has
`call_id`, `key_source`, `state`, `session_key` (null for a Call created under
`metadata_only` or keyed by its trace ID), first and last evidence times, `span_count`,
`recording_count`, `latest_revision` and `materialize_error`. `GET /v1/calls/<call_id>`
adds the Revisions, the Recordings, `evidence_after_latest_revision`, the transcript and
`transcript_coverage`: `available`, `not_captured_by_preset` under `metadata_only`, or
`not_observed`. To purge a Call, see [privacy](./privacy.md#calls).

The transcript has one line per Pipecat `stt` span with a transcript (the user) and one
per `llm` span's `output` (the agent). The `output` keeps the partial reply when the user
interrupts; a cancelled `llm` span gives no line. Pipecat 1.5's `tts.text` holds only the
last sentence of a reply, so Earshot does not use it for agent lines. For LiveKit, a user
line is each `user_turn`'s transcript and an agent line each `agent_turn`'s played text,
never an LLM reply LiveKit discarded.

**The call view.** Once the Call has closed and its Recordings are analysed (about half a
second for a 30-second call), the call detail's `projection` holds the turns with their
reported latency and audible gap, the signals, the cost and the placed transcript, and
`projection_state` is `current`. Open `http://127.0.0.1:4319/calls` for the calls list
and each call's player. [The call view](./calls.md) explains every field and the list's
filters.
