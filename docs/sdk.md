# Python SDK and recorder contract

The Python package is the v1 authority. Its default path is deliberately small:

```python
import earshot

earshot.configure(endpoint="http://127.0.0.1:4319")

with earshot.session(session_id="session-opaque") as incident:
    # Attach a framework adapter, or record facts from a raw pipeline.
    with incident.operation("agent", turn_id="turn-1"):
        run_voice_agent()

earshot.shutdown()
```

Calling `session()` without `configure()` is valid and uses metadata-only capture with
no exporter. Call `configure()` before creating sessions when enabling HTTP export,
non-default capture policy, or queue settings. Loopback HTTP export is allowed; remote
export endpoints require HTTPS. The exporter appends `/v1/incidents` unless the endpoint
already contains it.

## Recorder lifecycle

One `IncidentRecorder` produces one immutable final bundle:

```text
open -> record facts -> close(status) -> equivalent detached snapshot on repeated close
```

The recorder owns bundle/session identity, a process clock domain, capture policy,
adapter manifest, omission ledger, and optional bounded exporter. Explicit `close()`
validates the complete graph before returning. Used as a context manager, close/export
failures are captured in `last_export_error` and never replace an application
exception.

Accepted models and nested attribute/extension containers are detached from caller
ownership before commit. Each `close()` result is also detached from the recorder's
cached bundle, so mutating a nested Python dictionary in supplied data or a returned
snapshot cannot rewrite finalized evidence. Capture-governance mappings are likewise
snapshotted when the recorder is constructed.

`export_accepted` is `True` when the final artifact entered the bounded queue, `False`
when it was dropped because the queue/exporter was closed or full, and `None` when no
export was attempted. This makes fail-open evidence loss observable.

## Recording surface

- `add_participant()` and `add_stream()` establish session ownership and label the
  record with the capture class inferred from retained attributes (or a matching,
  enabled explicit class).
- `record_operation()` consumes a native or authored operation while preserving OTel
  identity, links, resource attributes/schema URL, scope name/version/attributes/schema
  URL, evidence, error, and allowed attrs.
- `operation()` is a manual context manager for a raw pipeline. It creates IDs but is
  not used by framework adapters that already emit OTel.
- `record_event()` records an independent point fact, optionally correlated to an
  operation/trace/span and its OTel resource/scope provenance.
- `record_quality_sample()` accepts a structured metric sample and recursively filters
  sample, evidence, resource, and measurement attributes. Measurement values may be
  numeric or boolean; raw counters are numeric only in v1.
- `add_media_ref()` attaches external audio metadata only when the `audio` class is
  enabled. It never embeds media bytes and removes credential-bearing locators.
- `add_raw_otlp_chunk()` retains exact bytes only when the `raw_otlp` class is
  enabled. This is an explicit caller-supplied path, not automatic OTLP interception.
  Callers cannot downgrade opaque OTLP bytes to metadata.
- `record_coverage()` records explicit unavailable/not-observed/unsupported facts and
  deduplicates a signal.

Participant/stream ownership, session references, graph identity, clocks, evidence,
privacy classes, and hashes are checked again when the bundle closes.
Pydantic model extras are recursively preflighted at recorder construction, adapter
registration, or the recording call. They are rejected unless `extension_payload` is
enabled, and non-portable values are rejected even when it is enabled, so an invalid
forward extension cannot first surface after the recorder has closed.
Existing Pydantic instances are serialized and revalidated at that boundary rather
than trusted merely because their outer type matches; this closes `model_copy()` and
nested-instance validation bypasses. Retained attribute maps receive the same strict
JSON, recursive privacy, credential, and unobservable-claim checks. A prospective
record must also fit the codec's 64-level profile depth before recorder state changes.

## Capture policy

Metadata is mandatory because the envelope cannot exist without it:

```python
from earshot import CaptureClass, CapturePolicy

policy = CapturePolicy(
    enabled=frozenset(
        {
            CaptureClass.METADATA,
            CaptureClass.TRANSCRIPT,
        }
    )
)
earshot.configure(capture_policy=policy)
```

The default allowlist keeps operational IDs, model/provider names, counts, finite
metrics, and selected resource identity. Transcript, audio, model/tool/diagnostic
payload, identity, raw OTLP, unknown free-form attributes, nested maps, and
credential-bearing URLs are excluded unless the correct class grants them. Unknown
normalized extensions use `extension_payload`; enabling `raw_otlp` only authorizes
exact opaque chunks. Third-party credential-free HTTPS OTel schema URLs and scope
attributes also require the extension grant; canonical versioned
`opentelemetry.io` URLs are metadata. Filtering covers nested error, evidence, link,
resource, scope, event, quality-measurement, and media structures.

Opaque IDs, service/model names, policy identifiers, and valid lowercase semantic
codes are trusted producer metadata; they must be pseudonymous/non-PII. The SDK hashes
suspicious free-form source/status/evidence/error labels, but it is not a semantic PII
classifier for an identifier that already satisfies its contract.

Caller-supplied capture labels are not trusted. The recorder infers the class of
retained attributes, rejects mixed sensitive classes in one record, and ensures the
privacy manifest says `captured=true` only for classes that actually survived.

Sensitive governance can be configured at the same boundary; callers do not need to
rewrite a closed bundle:

```python
from earshot import (
    CaptureClass, CaptureGovernance, CapturePolicy,
    ConsentConfig, ExportConfig, RetentionConfig,
)

policy = CapturePolicy(
    enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
    governance={
        CaptureClass.TRANSCRIPT: CaptureGovernance(
            consent=ConsentConfig(status="granted", legal_basis="consent"),
            retention=RetentionConfig(ttl_nano="3600000000000", policy_id="one-hour"),
            export=ExportConfig(allowed=False, destinations=("local_api", "local_cli")),
        )
    },
)
```

`CaptureGovernance` also accepts a versioned `RedactionConfig`. The recorder emits
these decisions into the matching `CaptureClassPolicy`; storage retention and every
named export destination enforce them.

The SDK exporter uses the destination name `sdk_http`. A captured class whose export
policy denies that destination is rejected before serialization enters the outbound
queue; `export_accepted` becomes `False`, and no network transport sees the artifact.

## Bounded exporter

`BoundedAsyncExporter.submit()` uses `put_nowait`; voice callbacks never perform
network I/O or wait for queue capacity. One worker performs bounded retries. Diagnostic
callbacks receive only a stable code, bundle ID, attempt, and retryability flag; they
run outside exporter locks and are exception-contained.
`shutdown(timeout)` may return `False` while a transport is still blocked, but it never
raises queue-capacity errors; the daemon worker drains and exits after release.

`HttpExportTransport`:

- allows HTTP only for loopback hosts;
- requires HTTPS for other hosts;
- rejects redirects instead of forwarding authorization;
- sends the canonical media type and bundle ID as the idempotency key; and
- classifies non-429 4xx responses as permanent.

## Framework adapters

Pipecat and LiveKit adapters are duck typed, so importing `earshot` does not import the
optional runtimes. Both attach a span processor to an existing provider, preserve
native trace identity, deduplicate callbacks, and remain fail-open. LiveKit also
attaches metrics/interruption listeners. Neither installs a second trace root.

The supported integration ranges are Pipecat `>=1.5.0,<1.6` and LiveKit Agents
`>=1.6.5,<1.7`. When LiveKit native spans and listeners are both attached, ownership is
type-selective: native spans own LLM, TTS, and realtime operations and embedded
metrics; EOU/EOT callbacks add quality/commit evidence; and STT, interruption, and
avatar callbacks remain operation sources because LiveKit 1.6 does not guarantee
equivalent native spans for them. A `VADMetrics` callback instead records one
`pipeline.metric` quality sample: per-emission inference duration/count are `delta`
measurements and idle time is an `instant` measurement. A native ended `vad` span
remains a `vad` operation. `LiveKitAdapter.consume_metric()` returns `None` only when
a VAD callback has no numeric measurement to retain; callback listeners stay
fail-open. Current LiveKit participant SID/kind become typed
participant ownership; identity remains governed payload. Pipecat's full `turn` span
remains a lifecycle container and is never mislabeled as endpoint detection.

Framework extras are optional:

```bash
pip install -e '.[pipecat]'
pip install -e '.[livekit]'
pip install -e '.[otel]'  # native OTel processors without a framework package
```
