# Security policy

Earshot processes telemetry that may contain recordings, transcripts, identities,
prompts, tool data, credentials, and operational metadata. Treat every incident and
every framework callback as sensitive input even when metadata-only capture is
configured.

## Reporting a vulnerability

Do not open a public issue containing a working exploit, secret, customer artifact,
or production identifier. Contact the maintainers privately and include:

- affected version/commit;
- impact and required attacker position;
- a minimal synthetic reproducer with unique sentinel data;
- whether the issue crosses capture, export, API, storage, analysis, or purge; and
- any temporary mitigation you verified.

The repository does not yet publish a dedicated security mailbox. Until one is added,
use the private security-reporting mechanism of the source host and avoid attaching
real voice-session data.

## Supported posture

- The API is local/single-node, not a multi-tenant authorization service.
- The M1 listener is loopback-only. Remote access requires a same-host
  HTTPS-terminating proxy to that loopback socket plus a bearer token; non-loopback
  API binding is rejected.
- HTTP export is accepted only on loopback; remote exporters require HTTPS and reject
  redirects.
- Metadata-only filtering happens before queueing or serialization, but applications
  must still avoid logging source callback values around Earshot.
- Purge is best-effort file erasure. Strong physical-media claims require
  cryptographic erasure and independently governed snapshots/backups.

## Regression expectations

A fix should add a synthetic sentinel regression covering every affected sink:
artifact JSON/protobuf, raw OTLP, CAS, SQLite/WAL, analysis, API/CLI error, exporter
diagnostic, and logs as applicable. Run:

```bash
pytest
pytest --cov=earshot --cov-report=term-missing
ruff check .
```

The security catalog is `fixtures/faults/security_regressions.json`.
