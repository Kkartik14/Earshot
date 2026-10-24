# ADR 0009: Price a call's reported usage as a lower bound, an estimate and a count of unknowns

Status: accepted (2026-10-20)

## Context

A developer looking at a voice call asks what it cost. The frameworks report part of
the answer on their spans, and leave the rest out:

- LLM requests report input and output tokens, usually. A request cancelled by a
  barge-in reports nothing, and LiveKit writes 0 for a count it did not get.
- TTS requests report characters (Pipecat's `metrics.character_count`, LiveKit's
  `lk.tts_metrics`).
- No framework reports how many seconds of audio a streaming STT billed (Pipecat 1.5,
  LiveKit 1.6 to 1.8).
- LiveKit's preemptive generation sends LLM requests whose replies are discarded. They
  were billed, though never spoken.

Prices change, differ by plan, and are sometimes promotional for a limited time. Some
models have no public price at all (Groq's Llama models). A team's negotiated price is
not the list price. Whatever Earshot shows must never present a guess as a measurement,
and must never show $0 for "we don't know".

Totals must also agree exactly on SQLite and Postgres, be filterable in the calls list,
and follow a price change without rebuilding anything.

## Decision

**Usage is read per request, from the stored spans, by a fixed usage → meter table**
(`earshot.call_span_facts`). Only these spans bill:

| Framework | Span          | Operation | Meters                                                               |
| --------- | ------------- | --------- | -------------------------------------------------------------------- |
| Pipecat   | `llm`         | llm       | `input_token`, `cached_input_token`, `output_token`                  |
| Pipecat   | `tts`         | tts       | `character`                                                          |
| Pipecat   | `stt`         | stt       | `audio_second` (never reported)                                      |
| LiveKit   | `llm_request` | llm       | as Pipecat `llm`; every request bills, discarded preemptive ones too |
| LiveKit   | `tts_request` | tts       | `character`; `input_token` and `output_token` when reported          |
| LiveKit   | `user_turn`   | stt       | `audio_second` (never reported)                                      |

Spans that repeat a request's usage (`llm_node`, `agent_turn`, `tts_node`, `*_run`,
`llm_fallback_adapter`) never bill. Input tokens include cached ones, so `input_token`
is input minus cached. Output tokens include reasoning tokens, which are never added
again. **A count of 0 is unknown**, never a quantity.

**Four statuses per cost item.**

| Status         | When                                                                                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `priced`       | The usage was reported, and a price row exists.                                                                                                                |
| `no_price`     | The usage was reported, and no price row matches.                                                                                                              |
| `not_reported` | A request left a primary meter unknown (LLM input or output tokens, TTS characters): one item per request.                                                     |
| `estimated`    | STT audio seconds: once per call and (provider, model), the user audio's duration (the recording's, else the call's), as a streaming STT bills connected time. |

**Three numbers per call, never one.**

- the **lower bound**: the sum of the `priced` items;
- the **estimate**: the sum of the `estimated` items, shown separately (`+ ~$0.004
(estimated)`), never folded into the lower bound;
- the count of **unknown items** (`no_price` plus `not_reported`).

A call's cost is **complete** only when it has a lower bound and no unknown item. A call
with no cost item at all has no total (NULL), never 0, and the viewer shows an
incomplete cost as `≥ $0.0046 · 2 unknown`.

**Money is integer nano-USD** (10⁻⁹ USD). A price is `price_nano_usd` per
`per_quantity` units: $0.075 per million tokens is `75` per `1`; $0.0077 per minute is
`7700000` per `60` audio seconds. Quantities are kept in thousandths of their unit. An
item costs `quantity × price / per_quantity`, rounded half up to a whole nano-USD, so
SQLite and Postgres agree to the nano-dollar.

**Prices are rows, append-only and effective-dated.** `prices(provider, model, unit,
price_nano_usd, per_quantity, effective_from_unix_nano, source_url, note, project_id)`:

- `provider` and `model` match exactly, lower-cased. Providers use the OpenTelemetry
  GenAI registry spelling the usage is normalized to (`api.groq.com`, `Groq` and `groq`
  are all `groq`).
- `project_id` NULL is a built-in row, for every project. A project's own row wins over
  a built-in one for the same (provider, model, unit).
- A call uses the row with the latest `effective_from_unix_nano` at or before its first
  evidence, so a new price never reprices calls that happened before it took effect.
- Rows are never edited or deleted, except a project's own rows with the project.
- Each insert bumps its scope's price version (the project's, or the built-in one).
  Every projection stores the versions it was priced at, so it turns stale and the
  projection job reprices it from its stored usage, in batches, without reading a span.

**Built-in prices are regular, dated list prices.** 0.3.0 seeds 32 rows for Deepgram,
Groq, OpenAI, Cartesia and ElevenLabs models, each with its source URL and the note
"regular list price as of 2026-10-19". Never a promotional, trial or plan-discounted
price: Deepgram Nova-3's limited-time $0.0048/min is not used, its regular $0.0077/min
is. Where a provider sells only by plan (Cartesia), the row is the Scale plan's overage
rate and says so. A model with no public price gets no row and prices as `no_price`.
The server seeds them at every start, idempotently, and never touches an operator's
rows.

**Prices are managed by the API and the CLI**, with no editor in the viewer:
`GET /v1/prices` and `POST /v1/prices` (project-scoped; hosted JWTs get 403), and
`earshot prices add (--project P | --builtin) …` and `earshot prices list`.

**The calls list filters on the lower bound.** `min_cost_nano_usd` compares the lower
bound. `max_cost_nano_usd` matches only **complete** costs (lower bound plus estimate
within the cap), because an incomplete cost may be anything above its lower bound; the
page then reports `excluded_incomplete`, the number of calls left out only because of
their unknown items.

## Consequences

Positive:

- Nothing unknown is shown as known: the viewer shows the lower bound, the estimate and
  the unknown count, and every item says where its number came from.
- Totals are exact and identical on both databases, and filterable through indexed
  integer columns.
- A price change, a project's own price or a newly published model price reprices
  every affected call from stored usage, with no migration.
- Hosts keep their own commercial terms: a project row overrides a list price without
  touching anyone else's.

Negative:

- The built-in table goes out of date. Its rows are dated, the operator can add newer
  ones (`earshot prices add --builtin`), and every release may add rows; no price is
  ever fetched from a provider at run time.
- STT is an estimate from audio duration, and does not model billing minimums (Groq
  Whisper bills at least 10 s per request).
- Realtime (speech-to-speech) models and audio-token usage are not priced in 0.3.0: they
  are not in the usage table.
- Cache writes have no unit of their own and stay inside `input_token`.

## Evidence

- Prices: the providers' public price pages, fetched 2026-10-19.
- Code: `packages/sdk-python/src/earshot/call_span_facts.py` (the usage table),
  `call_cost.py` (usage and pricing), `pricing_defaults.py` (the built-in rows and the
  choices behind them), `storage_prices.py`.
- Tests: `packages/sdk-python/tests/test_call_span_facts.py`,
  `packages/sdk-python/tests/test_call_projection.py` (the fixtures' exact cost items),
  `apps/ingest/tests/test_prices_api.py`, `apps/ingest/tests/test_prices_cli.py` and
  `apps/ingest/tests/test_call_projection_job.py` (repricing from stored usage, asserted
  to the nano-dollar on SQLite and on Postgres).
