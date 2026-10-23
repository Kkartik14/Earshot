# TVIC runtime adapter

Status: implemented metadata-only receiver for TVIC's bounded runtime-observation
envelope. This is an in-process adapter contract, not the separate hosted outbox
proposal in [`runtime-event-v1`](../contracts/runtime-event-v1.md).

TVIC owns execution and emits enqueue-only session/turn facts; Earshot validates the
envelope and maps accepted metadata into normalized events and coverage. The adapter
is bound to one Earshot session. A host bridge must route events by `sessionId`, keep
the TVIC callback non-blocking, and invoke Python ingestion outside the runtime's
execution path:

```python
adapter = TVICRuntimeAdapter.create(
    session_id="earshot-session-id",
    started_at_unix_nano=call_started_at_unix_nano,
)
sink = TVICRuntimeObservationSink(adapter)

# The host bridge forwards each JSON-shaped TVIC observation to:
sink.enqueue(observation)

bundle = sink.close()
```

The TypeScript runtime sink callback must only enqueue in bounded memory; it must not
call the recorder, perform I/O, or wait for the Python consumer. Runtime-wide drop
summaries cannot be attributed to one call and are not treated as per-call facts by
this adapter. The host needs a separate runtime-scoped health path for those summaries.

## Accepted facts and timing

The adapter accepts `session.start`, `session.resume`, `session.end`, `turn.start`,
`turn.end`, `turn.interruption`, and `observation.dropped`. It validates a closed
attribute allowlist and rejects unknown fields, unbounded identifiers, non-finite
times, malformed enums, stale chronology, and observations for another session.
Transcript, audio, prompts, model output, and provider payloads are not accepted.

TVIC `atMs` is session-relative monotonic time. The adapter clamps a recovery epoch to
the last observed coordinate; it does not add `recovery_gap_ms` a second time. A
sequence gap, explicit discontinuity, estimated terminal timestamp, rejected fact, or
missing interruption fact is retained as coverage and makes the incident incomplete.
`session.resume` marks committed durable recovery, not provider or media readiness. If
TVIC has evicted one detached session's bounded sequence state, only that session gets
a new epoch on resume; unrelated live session streams keep their epoch.
Malformed facts rejected by the bounded sink are recorded as ingress loss before close.
If TVIC can no longer use its local monotonic session clock at terminalization, it may
estimate elapsed time from persisted wall timestamps; the resulting evidence is marked
`estimated` and has no asserted clock uncertainty.

A `turn.end` with `terminal_persisted: false` records partial terminal-persistence
coverage but does not close the observed turn or emit its canonical end/latency events.
A later persisted `turn.end` supplies those events; if none arrives, close retains the
unmatched turn and marks the incident incomplete. An omitted persistence flag is
unknown, not proof of durability: Earshot records the observed terminal metadata but
marks the incident incomplete. Closing a session while a turn remains open also marks
the final incident incomplete, even when TVIC reports the session as completed.

`first_token_ms` and `first_audio_ms` are relative to TVIC's turn endpoint. Earshot
places `earshot.response.first_token` and
`earshot.response.first_audio_generated` events relative to that endpoint. These are
end-to-end response timeline facts, not provider-reported `earshot.llm.ttft` metrics.
Timeout interruptions are not attributed to the user; explicit, DTMF, and barge-in
facts are.

## Verification and capture status

The shared adapter conformance gate validates deterministic JSON/protobuf output,
incident semantics, and accepted event mapping. `packages/sdk-python/tests/test_tvic_adapter.py`
covers chronology, replay, bounded ingress, loss accounting, and failure paths. The
cross-runtime fixture in those tests is synthetic and does not stand in for a retained
real capture. See [`captured-fixtures`](../captured-fixtures.md) for that explicit gap.

For an opt-in live producer-to-consumer check, run the TVIC provider stack against
its configured real providers and pass its pseudonymized, metadata-only lifecycle
envelopes directly through this adapter:

```bash
python scripts/tvic_live_adapter_smoke.py --tvic-dir /path/to/TVIC
```

This check requires the TVIC checkout's configured Deepgram, Groq, and Cartesia
credentials. Its temporary envelope file is deleted when the command exits; audio,
transcripts, prompts, model output, provider payloads, and real identifiers are not
retained. It verifies ordered session/turn facts, a final complete Earshot incident,
semantic validation, and a usable turn analysis. It complements rather than replaces
the deterministic conformance tests.

```bash
python -m pytest packages/sdk-python/tests/test_tvic_adapter.py -q
python -m pytest packages/sdk-python/tests/test_adapter_conformance.py -q
```
