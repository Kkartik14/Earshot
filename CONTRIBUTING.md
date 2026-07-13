# Contributing to Earshot

Earshot's contract is evidence-first: preserve what was actually observed, label how
it was observed, and represent missing evidence explicitly. Do not manufacture a
stage, zero value, globally ordered clock, network-quality claim from PCM, client
render claim from server TTS, or human `heard_at` fact.

## Setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples/livekit_console
```

Node/pnpm are needed for the TypeScript compatibility packages and their CI gates.

## Contract changes

Update the Pydantic models, semantic validator, protobuf envelope when applicable,
semantic registry, generated JSON Schema, public docs, valid/invalid fixtures, and
round-trip tests together. Run:

```bash
python scripts/generate_contract.py
python scripts/generate_fault_fixtures.py
python scripts/generate_openapi.py
python scripts/check_semconv.py
```

Wrapper breaking changes require a schema-version decision. Semantic vocabulary is
open, but a new normalized value still needs documentation and an adapter fixture.

## Adapter changes

- Consume an existing framework OTel graph or observer seam; do not install a second
  trace root.
- Preserve trace/span/parent/link/resource/scope identity.
- Filter before queueing and record omissions without source values.
- Distinguish measured, estimated, inferred, unavailable, and not observed.
- Keep callbacks fail-open and free of synchronous network I/O.
- Add deduplication/conflict, privacy-sentinel, native-shape, and semantic-equivalence
  tests.

## Test standard

Prefer a test that breaks one invariant over a test that only constructs valid data.
Relevant changes should cover unit/property behavior, persistent restart, concurrency,
HTTP boundaries, and end-to-end leak scanning in proportion to risk. New production
faults belong in the deterministic fault corpus or security regression catalog.

Do not commit real transcripts, recordings, phone numbers, tokens, signed URLs, or
customer identifiers.
