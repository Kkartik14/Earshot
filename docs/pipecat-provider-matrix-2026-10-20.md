# Pipecat provider matrix — 2026-10-20

This live run exercised the full TVIC Pipecat provider product using Pipecat
`1.6.0` and real configured credentials. The checked-in runner is
[`scripts/run_pipecat_matrix.py`](../scripts/run_pipecat_matrix.py); the sanitized
machine-readable report is
[`pipecat-provider-matrix-2026-10-20.json`](./pipecat-provider-matrix-2026-10-20.json).
The report contains per-cell statuses, validation results, operation statuses,
measurement summaries, and artifact hashes; raw audio and source artifacts remain in
the ignored `.earshot` directory.

## Dimensions and outcome

The dimensions are STT (Deepgram `nova-3`, Cartesia `ink-2`, Sarvam `saaras:v3`,
ElevenLabs `scribe_v2_realtime`, AssemblyAI `u3-rt-pro`, Soniox `stt-rt-v5`), LLM
(Groq `openai/gpt-oss-20b`, OpenAI `gpt-4.1-mini`), and TTS (Cartesia `sonic-3.6`,
ElevenLabs `eleven_flash_v2_5`, Sarvam `bulbul:v3`). The product is 6 × 2 × 3 = 36
cells.

| Result           | Cells | Meaning                                                               |
| ---------------- | ----: | --------------------------------------------------------------------- |
| Completed        |    10 | Five non-Soniox STT providers × Groq × Cartesia or Sarvam TTS         |
| Timed out        |    26 | The live session did not complete before its timeout                  |
| Unavailable      |     0 | Every cell had its required environment and optional SDK dependencies |
| Invalid evidence |     0 | Every produced incident passed structural/semantic validation         |

The timed-out cells were all 18 OpenAI-LLM combinations, all 6 Groq + ElevenLabs-TTS
combinations, and the two Soniox + Groq combinations with Cartesia or Sarvam TTS.
“Valid evidence” means the captured incident conforms; it does not mean the provider
cell completed successfully. A timeout remains a failed live cell, even when its
partial incident is valid.

The separate boundary probes recorded HTTP 429 for OpenAI and HTTP 402 for ElevenLabs.
They are top-level diagnostic observations only and do not attribute an individual
matrix-cell timeout to either provider. The per-cell report deliberately has no invented
provider/stage attribution. In particular, Soniox cells timed out without a
provider-specific stage failure, so that path remains unresolved by this run.

## Latency interpretation

The 250 ms review target applies to local observation overhead, not remote-provider
response time. The run records Pipecat pipeline measurements; across the artifacts,
LLM TTFB ranged from 155.7 to 9,420.4 ms and TTS TTFB from 89.2 to 278.5 ms. These
remote/pipeline measurements do not establish the Earshot observer's own overhead.
That specific under-250-ms claim remains unverified by this matrix.

## Reproduction

Install the exact lock-backed matrix environment with
`uv sync --locked --extra pipecat-matrix`, then source the TVIC and Earshot environment
files in that order so Earshot's shared keys take precedence and TVIC-only provider
keys remain available:

```bash
set -a
source /path/to/TVIC/.env
source /path/to/earshot/.env
set +a
uv run --locked --extra pipecat-matrix python scripts/run_pipecat_matrix.py --force
```

This makes real provider requests and may incur charges or encounter account limits.
The runner returns nonzero if a cell is not completed or its evidence is invalid,
including when it reuses a cached artifact; `--force` reruns all available cells.
Missing credentials or SDKs are reported as unavailable, never as synthetic passes.
