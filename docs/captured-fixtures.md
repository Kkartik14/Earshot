# Retained real-capture fixtures

`fixtures/captured` contains privacy-scrubbed Incident artifacts produced by real
runtime or provider sessions. They complement synthetic conformance and fault fixtures:
their purpose is to prove that an advertised adapter can produce an artifact accepted by
the current public CLI and validator.

The machine-readable inventory is
[`fixtures/captured/manifest.json`](../fixtures/captured/manifest.json). A surface is not
listed as captured unless its source evidence came from a real execution. Missing source
evidence is recorded in `unavailable_real_captures`; synthetic payload tests do not fill
that gap.

## Currently retained

| Surface  | Source evidence                 | Captured   | Artifact                 |
| -------- | ------------------------------- | ---------- | ------------------------ |
| LiveKit  | `livekit-agents==1.6.5` session | 2026-07-17 | `livekit.incident.json`  |
| Pipecat  | `pipecat-ai==1.5.0` session     | 2026-07-17 | `pipecat.incident.json`  |
| Deepgram | Provider delivery               | 2026-07-19 | `deepgram.incident.json` |
| Cartesia | Provider delivery               | 2026-07-19 | `cartesia.incident.json` |
| Sarvam   | Provider delivery               | 2026-07-19 | `sarvam.incident.json`   |

The retained source artifacts used the obsolete pre-alpha labels `1.0.0` for the
contract and semantic profile. Their structures pass the current contract unchanged;
the retained copies therefore migrate only those labels to `0.1.0` before validation.
This is a compatibility-label correction, not a claim that arbitrary `1.0.0` artifacts
are supported.

## Capture and scrubbing policy

Real source captures remain in the gitignored `.earshot` area and are never committed
directly. Before an artifact enters `fixtures/captured`:

1. Capture runs with the metadata-only policy. Credentials stay in environment
   variables and provider request/response bodies are not retained.
2. Remove all raw OTLP chunks, media references, transcript, audio, identity, tool,
   model, and diagnostic payload classes. An artifact with any of those classes marked
   captured is ineligible for this directory.
3. Pseudonymize bundle/session, participant, turn, operation, event, sample,
   provider-correlation, service, voice, trace, and span identifiers while preserving
   equality, graph references, parentage, measurements, units, timing, and provenance.
4. Scan for credentials, authorization values, URLs with credentials, email addresses,
   and content-bearing fields. Do not rely on hashing a secret or transcript to make it
   publishable.
5. Run both `validate_incident()` and the public CLI. Commit the artifact only when both
   accept it without warnings or errors.

The committed artifact is evidence of the captured adapter behavior, not a recording of
the conversation. Audio files used to drive provider checks, raw provider deliveries,
and unredacted captures must remain outside version control.

## Verification

```bash
pytest -q packages/sdk-python/tests/test_captured_fixtures.py

for artifact in fixtures/captured/*.incident.json; do
  earshot validate "$artifact"
done
```

The test also requires every artifact to have operations, contain no raw OTLP or media
references, and capture no class other than metadata.

## Refreshing a fixture

Use the exact supported dependency range documented for the adapter, save the result to
the gitignored `.earshot` area, and record only the execution date and dependency version
in the manifest. Apply the scrubbing policy above, inspect the diff, then run the complete
fixture test and CLI loop. Replacing a real capture with a constructed dictionary is not
a refresh; keep such data under conformance or fault fixtures instead.

OpenAI Realtime and finalized-delivery Connectors currently lack retained real source
captures. Producing them requires an external provider session or signed provider
delivery, so their absence remains explicit in the manifest rather than being papered
over with synthetic evidence.
