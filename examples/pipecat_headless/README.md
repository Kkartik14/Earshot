# Headless Pipecat evidence run

This example exercises an in-app, cascaded voice agent without telephony, a room, a
microphone, or speaker hardware. It sends locally synthesized PCM through real Groq
STT, LLM, and TTS services, drains generated audio through a Pipecat output transport,
and captures Pipecat's native OpenTelemetry spans with Earshot.

The discard transport proves that server-side TTS audio reached the runtime's output
boundary. It does **not** prove browser/device render or that a person heard the audio;
Earshot records client render as `not_observed`.

## Requirements

- Python 3.11+
- macOS `say` for local input synthesis
- A `GROQ_API_KEY`; availability, rate limits, and billing depend on the current Groq
  plan

Install the example-specific optional dependency:

```bash
pip install -e '.[pipecat-groq]'
```

Set the key without committing it:

```bash
echo 'GROQ_API_KEY=gsk_...' >> .env
set -a && . ./.env && set +a
```

## Run

```bash
python examples/pipecat_headless/drive.py
```

The harness is intentionally one-shot: each run belongs in a fresh process. It fails
fast if another global OpenTelemetry provider is already installed, because silently
attaching Earshot to a rejected replacement provider would produce an empty artifact.

The final incident is written to `.earshot/pipecat_headless/incident.json`, which is
under the repository's ignored `.earshot/` tree.

Exit `0` means all of the following were proven:

- the pipeline started and terminated cleanly;
- trace flushing succeeded;
- the incident is contract-valid and has session status `completed`;
- native Earshot operations include STT, LLM, and TTS;
- the discard output transport accepted nonempty TTS audio.

A timeout, pipeline error, failed flush, missing stage, missing TTS audio, invalid
incident, or finalization failure exits nonzero. Once recorder creation succeeds, the
driver still attempts to write a truthful `failed` or `timed_out` incident and shuts
down every initialized component.

The run deadline is observed independently of task cancellation, so a coroutine that
returns after the deadline can never turn the incident back into success. Python cannot
forcibly terminate a coroutine that suppresses every cancellation forever; use an OS
process supervisor for an absolute wall-clock kill deadline in automation.

Earshot's default metadata policy filters prompts, transcripts, assistant text, PCM,
and credentials from the normalized artifact. The generated input audio exists only in
a temporary directory and is removed after synthesis. The harness disables Pipecat's
payload-bearing framework logs before service construction; its own failures use stable
stage messages and never echo provider exception text.

## Offline regression tests

The lifecycle suite uses injected runtime boundaries and makes no provider calls:

```bash
pytest packages/sdk-python/tests/test_pipecat_headless_driver.py -q
```
