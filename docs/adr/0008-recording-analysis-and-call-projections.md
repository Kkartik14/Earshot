# ADR 0008: Analyse each recording in a child process, and project each call from its evidence

Status: accepted (2026-10-20)

## Context

ADR 0007 made the Call the core object: spans and stored Recordings are the truth, and
each Revision is an immutable Incident built from them. 0.3.0 adds a call view that
needs more than a Revision holds:

- audio with a waveform, and a transcript on the audio's time axis;
- per turn, the latency the framework reports **and** the audible gap at the agent,
  measured from the recording: the time from the end of the user's speech to the start
  of the agent's speech, as the recorder heard it;
- signals across the call (talk time, overlap, barge-ins, silences);
- a cost (ADR 0009);
- a calls list that filters on all of these, and searches transcript text.

The audio work is heavy: decode, resample, a neural voice activity detector, peaks. It
needs numpy and onnxruntime, which the base and `[server]` installs do not carry, and
its memory and CPU grow with the audio. The OTLP receiver must stay fast while it runs
(ADR 0005), and the Revision must stay byte-deterministic (ADR 0007).

Recordings arrive after the spans, prices change after a call, and the computation
itself will improve. Whatever the view reads must follow all three without editing a
Revision.

## Decision

**Two background jobs, one per kind of input.**

```text
recording stored ──▶ audio_analysis (child process) ──┐
revision finished ─────────────────────────────────────┼──▶ call projection (pure) ──▶ call_projections
price added ───────────────────────────────────────────┘                               call_transcripts
                                                                                        call_tags
```

### The analysis job: one per stored recording, in a child process

- **Scheduling.** Storing a Recording inserts its `audio_analysis` row (`pending`) in the
  same transaction. Upgrading to 0.3.0 queues one for every Recording already stored.
  The server's maintenance loop leases due rows through a generic leased-job helper
  (`earshot.leased_jobs`): the lease is the job's timeout plus 60 s, and a write is
  conditional on the lease token, so a job whose lease was lost writes nothing.
- **Isolation.** Each job runs `python -m earshot.signals.child` as a process of its own
  (its own session, `nice 10`, one thread per numeric library, no core files, standard
  output discarded). It exits when its server dies. On Linux it is capped at 4 GiB of
  address space (`RLIMIT_AS`); macOS cannot cap it. The server never imports numpy or
  onnxruntime, and never runs analysis under the store's write lock. The child reads a
  copy of the audio spooled in `<data-dir>/analysis-tmp/`, swept at every start.
- **Timeout and retries.** 30 s plus 0.25 × the audio's duration, at most 1 hour (15
  minutes when the duration is unknown, or more than the file's bytes could hold). A
  crash, a timeout, an out-of-memory exit or any other failure after the claim (the
  spool, reading the Recording, writing the result) is retried after 1, 4 and 16
  minutes, then is `failed` with its code. A lease that runs out counts as a failed
  attempt (`EARSHOT_ANALYSIS_LEASE_EXPIRED`), so nothing is retried forever. A blob
  written after the job's outputs were reserved and drained is deleted. Audio the decoder cannot read, an
  unsupported codec, a missing `signals` extra or a model whose sha256 does not match is
  `unavailable` with its code: terminal, and never a guessed result.
- **What it computes**, per channel, in integer milliseconds from the file's first
  sample:
  - **speech runs:** Silero VAD v6.2.3 (ONNX, MIT, vendored and sha256-checked; CPU,
    one thread) on 512-sample frames at 16 kHz, speech on at 0.5 and off at 0.35,
    minimum speech 250 ms, minimum silence 200 ms, no padding;
  - **refined edges:** each run's start and end move to the nearest 10 ms energy frame
    above `max(−50 dBFS, noise floor + 15 dB)` within 100 ms. Each edge's uncertainty is
    σ = max(10 ms, |Silero edge − refined edge|), or 100 ms when no frame qualifies;
  - the noise floor, exact-zero runs of 50 ms or more, and peaks in audiowaveform `.dat`
    v2 (8-bit, 256 source samples per pixel).
- **Outputs.** The result document is stored on the row; the peaks are a blob. A user
  and an agent mono Recording on one timeline (the same `started_at`, durations within
  20 ms, and no other mono user or agent Recording at that start) also get one stereo
  FLAC rendition, so the pair plays as one file. Blob keys are journaled for deletion
  **before** any byte is written, so a crash or a purge mid-job leaves nothing behind.
- **Versioned.** Every result carries `ALGORITHM_VERSION`. A server with a newer
  algorithm requeues older results at start, in bounded batches.

### The projection job: a pure function of the call's evidence

- **Inputs.** The call's stored spans, its latest Revision's stored Recordings with their
  analyses, and the price rows. A Revision holds exactly the spans and Recordings stored
  when it was built (`first_revision` on each row, set when the Revision is claimed), so
  evidence that arrives later changes the projection only once a later Revision holds
  it, and `ended` follows that Revision's own close. It does not decode the Revision bundle: the bundle
  hashes some LiveKit names and rewrites providers, and the stored spans carry
  everything the view needs. `compute_projection` reads no clock, database or
  environment, so it runs outside every lock and a rerun gives the same projection.
- **Triggers, as a counter.** `calls.projection_seq` is bumped in the same transaction
  as each finished Revision and each analysis result. A projection stores the
  `projection_seq`, the price versions and the `CALL_PROJECTION_VERSION` it was computed
  from, and is `stale` when any of them has moved, `current` otherwise. A stale
  projection is still served.
- **Compare-and-set.** The job reads one snapshot, computes outside the lock, and writes
  in one short mutation only if the call's `projection_seq`, its latest Revision and its
  stored projection are still what it read. Otherwise it discards the result and a
  later pass recomputes it. A projection whose `input_digest` (sha256 of its inputs) is
  unchanged only has its stamps moved; one stale only because prices changed is
  repriced from its stored usage without reading a span.
- **What it holds.** The list's filter columns (agent, outcome, tags, duration, turn
  counts, reported latency and audible gap p50 and max, signals, cost) and a compact
  detail document (media, turns, signals, cost items, the placed transcript), plus one
  `call_transcripts` row per line for search and one `call_tags` row per tag. Unknown is
  NULL, never 0, and no filter matches NULL.

### The audible gap at the agent

- **One recorder timeline only.** The two channels of one stereo Recording, or a mono
  pair on one timeline. Anything else is `not_measurable / not_one_timeline`.
- **Pairing.** The framework's agent-on anchor (Pipecat: the first TTS start plus its
  time to first byte; LiveKit: the `agent_speaking` start) finds the agent's speech run:
  for Pipecat, whose recorder pads the agent track with digital zero, the edge of the
  nearest exact-zero run within 1.5 s; for LiveKit, whose recorder places frames by wall
  clock, the anchor itself. The agent run starting nearest it within 1 s is paired with
  the last user run ending before it, with no agent speech between.
- **Value.** `agent run start − user run end`, ± √(σu² + σa²).
- **Uncertainty limit.** When the combined boundary uncertainty exceeds 100 ms, the gap
  is `not_measurable / boundary_uncertainty_too_high`; its edge times and uncertainty
  remain available, but no gap value is reported.
- **Discontinuities.** Pipecat's recorder only ever inserts time. When the user-off and
  agent-on anchors map onto the recording with offsets more than 150 ms apart, the
  recorder inserted time inside the gap: the turn is
  `not_measurable / recorder_timeline_discontinuity`, and the raw gap is shown as an
  upper bound ("≤ 3.08 s"). The step is never subtracted. LiveKit turns are measured on
  the basis `continuous_timeline_assumed`; Pipecat turns with both anchors on
  `anchors_checked`.
- **Never fabricated.** Every other case is `not_measurable` with its reason
  (`no_agent_audio`, `no_clear_pairing`, `no_recording`, `analysis_not_stored`), or
  `talk_over` when the agent started while the user was speaking.

## Consequences

Positive:

- OTLP ingest and the closer do no audio work. A crashing, hanging or memory-hungry
  analysis costs one child process, not the server.
- Revisions stay immutable and byte-deterministic. Late audio, new prices and better
  algorithms change the projection, never a Revision.
- A projection can always be recomputed from what is stored, so a version bump or a
  price change converges without a migration.
- The same input on the same platform gives identical runs; on both fixtures the runs
  and σ are also identical on macOS arm64, Linux aarch64 and Linux x86_64.
- The uncertainty is reported as measured. On the Pipecat fixture, turn 2 is
  1.51 s ± 75 ms, because Silero's end lags the energy edge; narrowing it to meet a
  target would overstate precision.

Negative:

- The call view lags a closed call by the analysis and one projection pass (about half
  a second for a 30-second call on a laptop).
- Without the `signals` extra, every analysis is `unavailable` with
  `EARSHOT_SIGNALS_EXTRA_REQUIRED` and no turn is measured. Installing it requeues them at
  the next start.
- Two copies of a mono pair's audio are kept (the tracks and the rendition), and the
  peaks are a further derived blob. All of them are purged with the Call.
- The audible gap is the gap **at the agent's recorder**. It is not what a caller heard
  over a network, and its accuracy on natural human speech is not yet measured against
  labels (the fixtures' callers are synthesized speech).
- At 8 kHz (telephony) Silero's edges are less precise; on the fixture they moved up to
  about 160 ms from the 16 kHz edges.

## Evidence

- Code: `packages/sdk-python/src/earshot/signals/`, `storage_analysis.py`,
  `leased_jobs.py`, `call_timeline.py` (module docstring: the gap and the signals),
  `call_projection.py` and `storage_call_projections.py`.
- Tests: `packages/sdk-python/tests/test_signals_analysis.py`,
  `packages/sdk-python/tests/test_call_projection.py` (the fixture goldens),
  `apps/ingest/tests/test_analysis_job.py`, `apps/ingest/tests/test_call_projection_job.py`,
  and the replay e2e `apps/ingest/tests/e2e/test_otlp_replay_quickstart.py`.
- Fixtures: `fixtures/captured/pipecat-otlp-1.5/`, `fixtures/captured/livekit-otlp-1.8/`.
