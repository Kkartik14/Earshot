# Python SDK and recorder contract

The Python package is the pre-v1 authority. The convenient process-global path and the
explicit-client path use the same implementation:

```python
import earshot

earshot.init()  # reads EARSHOT_* configuration

with earshot.conversation(session_id="session-opaque") as incident:
    # Attach a framework adapter, or record facts from a raw pipeline.
    with incident.operation("agent", turn_id="turn-1"):
        run_voice_agent()

assert earshot.flush(timeout=5.0)
earshot.shutdown(timeout=5.0)
```

`init()` reads `EARSHOT_ENDPOINT`, `EARSHOT_TOKEN`, `EARSHOT_PROJECT_ID`, queue byte/count
limits, compression threshold, root sampling, and delivery-mode settings when the
matching keyword is omitted. Calling `conversation()` or `session()` without an endpoint
is valid: it records metadata locally and performs no export. Loopback HTTP export is
allowed; remote endpoints require HTTPS. Endpoint URLs reject userinfo, query strings,
fragments, malformed ports, whitespace, and control characters. `/v1/incidents` is
appended unless already present.

Libraries, tests, multi-project processes, and applications that need explicit resource
ownership should construct a client instead:

```python
client = earshot.Client(
    endpoint="https://observability.example",
    token=load_secret(),
    project_id="voice-production",
)

with client.conversation(session_id="session-opaque") as incident:
    run_voice_agent(incident)

health = client.status()
assert client.shutdown(timeout=5.0)
```

Equivalent initialization is idempotent. Conflicting global reinitialization is rejected
while a recorder is active, so an older conversation can never be silently redirected or
stranded. `configure()` remains a compatibility alias that returns a non-secret config;
new code should prefer `init()` or `Client`.

Root sampling is deterministic for `(seed, project, conversation ID)`, and all causal
children follow the root decision. `suppress_instrumentation()` is context-local and is
used around Earshot's own network calls to prevent recursive self-capture. Conversation
and current-operation context use token-based restoration and remain isolated across
interleaved async tasks and threads.

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

A final-validation failure is terminal: the recorder becomes closed, releases its
client ownership exactly once, exports nothing, and repeated `close()` raises the same
failure. This prevents a malformed recorder from blocking process reconfiguration or
being repaired later and accidentally routed through a different project. Validate
producer references before the final close boundary when recovery is required.

Accepted models and nested attribute/extension containers are detached from caller
ownership before commit. Each `close()` result is also detached from the recorder's
cached bundle, so mutating a nested Python dictionary in supplied data or a returned
snapshot cannot rewrite finalized evidence. Capture-governance mappings are likewise
snapshotted when the recorder is constructed.

`export_accepted` follows the selected delivery boundary. It means memory-queue
acceptance in `async` mode, remote acknowledgement in `sync` mode, and an atomic local
disk commit in `durable` mode. It is `False` when that boundary fails and `None` when no
export was attempted. Remote delivery after a durable commit remains separately
observable through `status()`.

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
  numeric or boolean; raw counters are numeric only in v1alpha1.
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

### Recorder memory limits

Every recorder has frozen, non-secret limits. The `0.1.0` defaults are exactly:

| Setting / environment variable                      | Default | Meaning                                                                       |
| --------------------------------------------------- | ------: | ----------------------------------------------------------------------------- |
| `max_records` / `EARSHOT_MAX_RECORDS`               |  10,000 | admitted evidence records, including adapters and explicit coverage/omissions |
| `max_capture_bytes` / `EARSHOT_MAX_CAPTURE_BYTES`   |  16 MiB | deterministic logical size of retained records and their privacy ledger       |
| `max_raw_otlp_bytes` / `EARSHOT_MAX_RAW_OTLP_BYTES` |   8 MiB | cumulative raw OTLP payload bytes inside that total                           |
| `max_value_bytes` / `EARSHOT_MAX_VALUE_BYTES`       |  64 KiB | one structural attribute/extension value before copying                       |

The byte count is a deterministic portable-structure estimate, not CPython heap size.
An oversized nested value is omitted before deep-copy/model work and its enclosing
record can still be admitted. If a whole record would exceed the record, total-byte, or
raw-byte cap, that record and every later evidence mutation are omitted. This
prefix-preserving freeze prevents a dropped parent from being followed by retained
children and makes the final incomplete bundle graph-valid and exportable. Capacity is
fail-open: it never raises into application or voice code. Boolean capture methods return
`False`; void methods become no-ops. Methods whose historical return type is a contract
record return the normalized _attempted_ record, which is not proof of retention.
`IncidentRecorder.status()` is the authoritative source of truth and reports capture
bytes/records, freeze state, first stable limit reason, fixed-size aggregate counters,
and estimated omitted bytes.

At close, truncation sets manifest `completeness="incomplete"` and emits one aggregated
`recorder.capture` coverage fact plus bounded privacy omissions. The client aggregates
`truncated_conversations` and `truncated_records` separately from exporter `lost` counts;
the one-shot `recorder.capture_truncated` diagnostic contains no captured value and runs
outside recorder locks with callback exceptions contained.

These caps bound open-recorder memory only; they are not crash recovery. With
checkpointing disabled — the default — persistence begins after final close even in
durable delivery mode, so a process crash before close loses the unfinished
conversation. Setting `checkpoint_dir` changes that: see [Crash recovery](#crash-recovery).

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
earshot.init(capture_policy=policy)
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

## Delivery modes and loss visibility

The default `delivery_mode="async"` is the normal long-running-process mode.
`BoundedAsyncExporter.submit()` uses `put_nowait`; voice callbacks never perform network
I/O or wait for queue capacity. One worker performs bounded, deadline-limited retries.
Both item count and bytes are capped.

`delivery_mode="sync"` performs the bounded send before `close()` returns and creates no
worker thread. It is intended for serverless invocations and scripts where remote
acknowledgement at the close boundary matters more than callback latency.

`delivery_mode="durable"` requires an explicit `spool_dir`. It persists the already
privacy-filtered whole incident before delivery and replays retained incidents after a
restart. The spool is plaintext operational evidence by default: put it on a private,
encrypted volume and treat directory access as sensitive. Optionally, each record's
payload can be envelope-encrypted at rest with AES-256-GCM by configuring a 32-byte
spool key (`spool_key`, `EARSHOT_SPOOL_KEY`, or `EARSHOT_SPOOL_KEY_FILE`) with the
optional `earshot-observability[spool-encryption]` extra (the `cryptography` dependency)
installed; with no key configured the spool stays plaintext and behavior is unchanged.
The explicit directory is the plaintext storage opt-in, must be owner-private, and
contains atomic, fsynced `0600` records.
Corrupt or crash-interrupted records are quarantined and surfaced as abandoned. Item
and byte limits are mandatory; retryable failures remain replayable, while permanent
rejections are retained by default or deleted only with an explicit `delete` policy.
One spool directory belongs to one client process; it is not a coordinated multi-process
work queue. Backend idempotency makes duplicate replay safe, but deployments with
multiple workers should give each worker its own spool directory or use an external
durable transport.
Records are bound to a fingerprint of normalized endpoint plus project (credentials are
excluded, so safe token rotation can drain the same route). A new route never sends an
older route's files. Those foreign records still count against the directory-global caps
and appear as abandoned pressure; recover them by reopening the same private directory
with their original endpoint and project identity.

Durability begins only after a recorder closes and its canonical incident is atomically
committed to the spool, unless checkpointing is enabled; see [Crash recovery](#crash-recovery).

`status()` reports lifecycle state, accepted/sent/dropped/failed/rejected/retried and
overflow counts, pending count, queued and in-flight bytes, byte high-water mark, oldest
age, last success, a non-secret failure code, sampling counts, and recorder truncation
counts. Recorder truncation is not included in exporter `lost`. Durable mode also
reports retained spool pressure. Diagnostic callbacks receive only a stable code, bundle
ID, attempt, and retryability flag; they run outside exporter locks and are
exception-contained. Durable fields are `spool_depth`, `spool_bytes`, `replayed`, and
`abandoned`. The settings also have `EARSHOT_DELIVERY_MODE`, `EARSHOT_SPOOL_DIR`,
`EARSHOT_SYNC_DEADLINE_SECONDS`, `EARSHOT_MAX_SPOOL_ITEMS`,
`EARSHOT_MAX_SPOOL_BYTES`, and `EARSHOT_PERMANENT_REJECTION_POLICY` environment forms.

`flush(timeout)` and `shutdown(timeout)` return `False` when the deadline expires. They do
not turn delivery failure into an application exception. Repeated shutdown is safe.
If reconfiguration activates a new exporter but the previous exporter cannot retire
within its lifecycle deadline, reconfiguration raises with an explicit partial-success
message; the new non-secret config is active and `status().state` remains `closing` until
the old resource can be retired.
Forked children recreate SDK-owned synchronization, queues, workers, and identity instead
of inheriting a dead parent worker or replaying an in-memory parent queue. A bounded atexit
handler attempts final shutdown, but correctness-sensitive applications should always
flush explicitly.

`HttpExportTransport`:

- allows HTTP only for loopback hosts;
- requires HTTPS for other hosts;
- rejects redirects instead of forwarding authorization;
- sends the canonical media type and bundle ID as the idempotency key;
- asserts `X-Earshot-Project-Id`, which the Earshot backend checks against the project
  selected by the credential so configuration cannot silently route to another tenant;
- retries 408, 429, and 5xx responses with bounded jitter and `Retry-After`; and
- classifies other 4xx responses as permanent.

## Crash recovery

Durability begins as soon as a fact is admitted, when checkpointing is enabled. Setting
`checkpoint_dir` (or `EARSHOT_CHECKPOINT_DIR`) opens one append-only, owner-private
journal per conversation and writes every admitted, already-privacy-filtered record to it
in admission order. Each record is a self-delimiting, CRC-protected frame, optionally
envelope-encrypted with AES-256-GCM under the same key precedence as the spool
(`checkpoint_key`, `EARSHOT_CHECKPOINT_KEY`, `EARSHOT_CHECKPOINT_KEY_FILE`, then the
spool key). Because each append reaches the kernel before the call returns, **a process
crash, forced termination, or out-of-memory kill loses no admitted evidence**; only a
host-level failure (kernel panic or power loss) can lose work, bounded by the fsync
window (`checkpoint_fsync_mode`, `interval` at 250 ms by default; `always` and `never`
are the other modes). The journal is bounded and never rotates: on reaching its cap
(`checkpoint_max_bytes`) it records the reason and stops rather than silently dropping
facts.

`earshot recover --checkpoint-dir DIR` reconstructs an incident from a journal, and
`earshot checkpoints list DIR` identifies the journals a directory holds. If the journal
contains the recorder's finalize record, the reconstructed artifact is byte-identical to
the one `close()` produced, so re-ingesting it deduplicates instead of conflicting. If it
does not, the artifact is explicitly **provisional**: `manifest.finality` is
`provisional`, `manifest.completeness` is `incomplete`, `session.status` is `interrupted`,
`session.ended_at` is absent because the real end was never observed, `manifest.recovery`
records the method, reason, journal identity, last durable observation, and any torn
trailing bytes, and coverage records `recorder.session_close` as unavailable. Validation
enforces this: a recovered incident that claims a clean close is rejected, so a
provisional artifact can never be mistaken for a final one. Operations that started but
were never observed to finish appear with no end time and status `unknown`, and the
analyzer reports their durations as unavailable rather than as zero.

Checkpointing is off by default. The explicit directory is the storage opt-in, must be
owner-private, and holds only the same capture classes the configured policy already
admits — enabling checkpointing never widens what earshot retains. One checkpoint
directory belongs to one client process, exactly like the spool. On a clean close the
journal is removed once the incident reaches a durable successor; a journal left behind
by a crash between finalize and cleanup recovers to a byte-identical duplicate, which
content-addressed ingest deduplicates.

## Capture seam: `ObservationSink`

A capture source authors governed facts through `earshot.observation.ObservationSink`
and depends on nothing else in the recorder. The protocol is exactly five verbs --
`record_measurement`, `record_event`, `record_coverage`, `record_omission`,
`register_clock_domain` -- and `TurnRecorder` satisfies it structurally, so no capture
source is required to own a pipeline session.

That is what makes the WebRTC and audio-graph engines runnable anywhere: `apply_*`
takes a sink, not a turn.

```python
from earshot.engines.webrtc import apply_webrtc_stats

class CollectorSink:  # a browser/native/backend collector's own sink
    def record_measurement(self, name, value, **fact): ...
    def record_event(self, name, **fact): ...
    def record_coverage(self, signal, availability, reason=None): ...
    def record_omission(self, field_name, *, capture_class, reason="adapter_payload_omitted"): ...
    def register_clock_domain(self, domain): ...

apply_webrtc_stats(CollectorSink(), snapshots)
```

Stage/operation authoring (`record_stage`) is intentionally not part of the protocol:
it mints an operation id and advances the pipeline's turn cursor, which is turn
bookkeeping rather than an observation.

## Export seam: named exporters

A finished incident leaves through a _named_ exporter. `otlp` and `openinference` are
registered built-ins; a user registers their own beside them and selects it the same
way, from the client or the CLI, without importing a projection module.

```python
import earshot

document = earshot.export(bundle, format="otlp")

earshot.register_exporter("acme", lambda bundle: {"acme": ...})
earshot.export(bundle, format="acme")
earshot.exporter_formats()  # ('acme', 'openinference', 'otlp')
```

`register_exporter(name, exporter, *, destination=None, replace=False)` defaults the
export destination to the exporter's own name, so a new exporter never inherits a
permission written for another backend; both built-ins declare `otlp`. A named export
is checked with `assert_export_allowed` against that destination before the projection
runs, and a duplicate name is an error unless `replace=True`. Registration performs no
I/O. `earshot.exporters.to_otlp` / `to_openinference` remain importable and unchanged;
the caller who imports them directly is then the one holding the export policy.

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
