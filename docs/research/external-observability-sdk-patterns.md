# External observability SDK patterns for Earshot

Date: 2026-07-22  
Method: first-party source and test inspection, not a README-only survey

## Executive conclusion

The mature SDKs do not win because they own an HTTP endpoint. An endpoint, a batch queue, and an `@observe` decorator are now commodity infrastructure. They win because they combine four properties:

1. **A very small adoption surface**: one initialization path, environment-based configuration, and instrumentation that fits the host framework.
2. **A deep, boring reliability core**: bounded memory, failure isolation, context propagation, explicit flush/shutdown behavior, process-fork handling, and diagnostics for discarded data.
3. **A stable semantic contract**: client-generated trace identity, parent/child rules, versioned payloads, and compatibility paths that let integrations evolve without rewriting user code.
4. **A product-specific workflow**: LangSmith turns traces into runs, datasets, experiments, and evaluations; Langfuse connects OTel traces to prompts, generations, scores, media, and datasets; Sentry turns telemetry loss and application failure into an operational workflow; OpenTelemetry is the neutral interoperability layer rather than the final workflow.

The fourth property is the defensible one. The first three are the price of admission.

For Earshot, the durable product thesis should therefore be narrower and stronger than “LangSmith for voice.” It should be: **a provider-neutral evidence system for explaining voice-agent behavior from causal, timestamped boundaries, with explicit provenance and uncertainty**. Generic trace products can display spans and are already adding voice adapters. LangSmith's current SDK, for example, contains a Realtime adapter that builds turns, transcripts, latency, interruptions, usage, and a stereo WAV from OpenAI events ([adapter design](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/integrations/openai_realtime/_connection.py#L1-L27), [event translation](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/integrations/openai_realtime/_connection.py#L186-L229), [session wrapper](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/integrations/openai_realtime/_connection.py#L408-L505)). Earshot must own the cross-provider voice truth model, not merely offer another audio-aware tree view.

## Scope and frozen revisions

The repositories were shallow-cloned under the git-ignored `local/sdk-research/` directory. All links below are pinned to the inspected commit; they do not float with a branch.

| SDK                  | Inspected commit                                                                                                  | Why it matters here                                                                                                               |
| -------------------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| LangSmith Python SDK | [`b310221`](https://github.com/langchain-ai/langsmith-sdk/tree/b3102211cc66680910b6d64992fece19fb68eadf)          | High-level tracing decorators, provider wrappers, batching, sampling, evaluations, and newly added voice adapters                 |
| Langfuse Python SDK  | [`11a232d`](https://github.com/langfuse/langfuse-python/tree/11a232d0eec6e2d25298e7d727aa546b48fd6393)            | OTel-based public SDK, generations/scores/prompts/media, global and explicit clients, multi-project safety                        |
| OpenTelemetry Python | [`1a71171`](https://github.com/open-telemetry/opentelemetry-python/tree/1a71171af64a78cc70e61dbe48630644f4b265a8) | Vendor-neutral API/SDK seam, context propagation, sampling, processors, OTLP transport, compatibility discipline                  |
| Sentry Python SDK    | [`3a50950`](https://github.com/getsentry/sentry-python/tree/3a50950e632d9923f45a9b6fb18d89e1f27badad)             | Mature auto-instrumentation, privacy controls, bounded transport, rate limits, lost-event accounting, broad compatibility testing |

Phoenix/OpenInference was not needed for this report. Its main distinct contribution—OpenInference semantic conventions and OTel instrumentation—is already represented by the OTel-first Langfuse design and the hybrid OTel path in LangSmith. Adding it would increase breadth without changing the recommendations.

“What users value” below is an inference from where these projects have invested durable public API, implementation complexity, and regression tests. It is not a substitute for interviews or usage telemetry.

## Comparison matrix

| Concern                | LangSmith                                                                                                                     | Langfuse                                                                                                       | OpenTelemetry Python                                                        | Sentry Python                                                                            | Earshot lesson                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Lowest-friction entry  | `@traceable`, environment config, explicit `Client`, provider wrappers                                                        | `@observe`, `get_client()`, explicit `Langfuse`, OTel instrumentation                                          | API works before SDK setup through proxy/no-op providers                    | `sentry_sdk.init(...)` installs a global client and auto integrations                    | Support a two-line default and an explicit-client path; both must use the same core                            |
| Public abstraction     | Domain-rich run types and tracing context                                                                                     | Typed observations: generation, agent, tool, chain, retriever, embedding, evaluator, guardrail                 | Small neutral `Tracer`/`Span`/processor/exporter interfaces                 | Global capture API plus request/current/global scopes                                    | Keep Earshot's public kernel small; voice vocabulary belongs in canonical events, not dozens of client methods |
| Context                | `ContextVar`-backed run tree and tracing context; wrappers preserve async context                                             | OTel current span plus a public-key `ContextVar`                                                               | `ContextVar` runtime context, W3C propagation                               | Separate global, isolation, and current scopes backed by context variables               | Model process config, conversation isolation, and current operation as separate layers                         |
| Integration strategy   | Decorators, monkey-patched clients, framework adapters, realtime connection proxies                                           | Decorator plus OTel/OpenInference/LangChain/OpenAI integrations                                                | Instrumentation libraries depend only on API; SDK/exporter is late-bound    | Default and auto-enabling integrations patch frameworks                                  | Provider adapters must be thin translation modules around a canonical Earshot kernel                           |
| Buffering/backpressure | Bounded priority queue, batching/compression, rate-limited drop warning                                                       | OTel batch processor plus 100k score/media queues and warnings                                                 | Bounded deque; oldest item drops when full; optional internal metrics       | Bounded queue; rejected enqueue is counted as `queue_overflow`                           | Never allow unbounded voice-session state; expose drop counts and reasons, not only logs                       |
| Retry                  | Requests retry config and explicit backoff; batch retries                                                                     | OTLP exporter behavior plus separate consumer retries                                                          | OTLP transient-status retries with jitter and interruptible shutdown        | Primarily rate-limit/backoff handling; failed/overflowed data is accounted as loss       | Specify delivery semantics; use idempotency if retrying whole batches                                          |
| Shutdown/fork          | Explicit `flush`, `cleanup`, `close`, weak finalizer/atexit; no equally clear process-fork reinit in inspected tracing client | `flush`, `shutdown`, atexit, OTel processor fork safety, explicit recreation of queues/HTTP clients after fork | atexit providers, processor fork reinit, queue clearing, resource refresh   | PID-aware worker restart, atexit close, fork regression tests and compatibility warnings | Test Gunicorn/uWSGI/preload, serverless, cancellation, interpreter exit, and repeated shutdown before release  |
| Sampling               | Trace-level sampling with children following the root decision                                                                | OTel trace-id ratio sampling; scores follow trace sampling                                                     | deterministic trace-id ratio and parent-based samplers                      | explicit > callback > inherited parent > configured rate; lost spans counted             | Sample whole conversations deterministically; never independently sample causal children                       |
| Privacy                | hide inputs/outputs/metadata, custom anonymizer, curated secret anonymizer; voice byte scrubbing/caps                         | custom attribute mask and batch-level OTel mask, media handling                                                | deliberately policy-neutral                                                 | PII off by default, key denylist scrubber, `before_send` hooks                           | Audio, transcript, metadata, and raw provider payload require separate opt-ins and fail-closed masking         |
| Compatibility          | legacy env names/modes, deprecated args, server capability fallback, hybrid OTel                                              | generated API namespaces, legacy endpoints, deprecation warnings, OTel as wire seam                            | API/SDK split, proxy late binding, instrumentation scope/version/schema URL | very broad framework/Python/version test matrix and gradual deprecations                 | Freeze a minimal v1 contract and test old adapter fixtures against every new core                              |
| Lost-event visibility  | rate-limited warning with aggregate drop count                                                                                | queue/filter diagnostics mostly through logs                                                                   | queue warnings and opt-in internal metrics                                  | client reports encode reason/category/quantity and are sent later                        | Make loss a first-class SDK health record retrievable locally and exportable                                   |
| Distinct product value | traces connected to experiments, evaluations, datasets and provider-native wrappers                                           | OTel interoperability connected to prompts, scores, media, datasets and cost                                   | portable telemetry contract                                                 | pervasive crash/performance capture plus operational issue workflow                      | causal voice diagnosis with evidence quality, not a generic span UI                                            |

## What the mature implementations actually teach

### 1. One easy path and one explicit path should converge

LangSmith exposes both an injected `Client` and ambient tracing. Its `traceable` decorator handles sync functions, coroutines, generators, async generators, and streaming objects while preserving the wrapped function and re-raising application exceptions ([public decorator](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/run_helpers.py#L330-L486), [async/sync wrapper behavior](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/run_helpers.py#L598-L850)). Langfuse makes `@observe` similarly easy, but resolves the right singleton client per public key and disables ambiguous multi-project tracing instead of risking cross-project leakage ([decorator API](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/observe.py#L44-L202), [safe client resolution](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/get_client.py#L64-L139)). Sentry's `init` constructs a client and binds it to the global scope, after which framework integrations can capture without threading a client through application code ([initialization](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/_init_implementation.py#L51-L61), [integration setup](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/client.py#L675-L708)).

The mature pattern is not “global client” or “explicit client.” It is **a single implementation reachable through both**. Earshot should offer:

```python
import earshot

earshot.init()                         # env-configured process default

with earshot.conversation(provider="livekit") as conversation:
    ...
```

and:

```python
client = earshot.Client(...)
with client.conversation(...) as conversation:
    ...
```

Decorators and provider wrappers should resolve a client once at the conversation boundary and then carry it explicitly in internal state. Ambiguity between multiple projects must fail closed, as Langfuse does, rather than silently choosing the first client.

OpenTelemetry demonstrates why this convergence needs an interface seam: instrumentation can import the API and acquire a proxy tracer before any concrete SDK is installed; once a real provider is configured, existing proxies delegate to it ([proxy/no-op provider](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-api/src/opentelemetry/trace/__init__.py#L215-L267), [global provider late binding](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-api/src/opentelemetry/trace/__init__.py#L524-L580)). Earshot does not need separate distributions immediately, but its provider integrations should depend on a narrow capture protocol rather than importing storage, HTTP, analysis, and configuration internals.

### 2. Context is a data-isolation feature, not merely convenience

OpenTelemetry's runtime context is a small token-based `ContextVar` adapter: attach returns a token and detach resets exactly that prior state ([implementation](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-api/src/opentelemetry/context/contextvars_context.py#L14-L47)). Langfuse adds a separate context variable for project selection so nested decorators use the correct client ([project context](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/get_client.py#L10-L34)). Sentry goes further: it distinguishes process-global data, request/user isolation, and the current span, then forks and restores those scopes independently ([scope layers](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scope.py#L108-L128), [current-scope lifecycle](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scope.py#L2032-L2095), [isolation scope](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scope.py#L2106-L2151)).

Earshot should have three explicit internal layers:

- process configuration and resources;
- conversation/session isolation, including project and privacy policy;
- current causal operation/turn/provider event.

Every enter must have token-based restoration in `finally`; every async task must inherit the intended conversation context; and adapters must test concurrent conversations with interleaved events. A mutable global “current call” is unacceptable for voice workloads.

Distributed propagation should use W3C trace context for correlation, but Earshot-specific conversation identity and evidence fields should be namespaced baggage with strict size limits. OTel's global `inject`/`extract` delegates to configured propagators rather than baking transport rules into spans ([propagation facade](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-api/src/opentelemetry/propagate/__init__.py#L70-L105)); Earshot should follow that separation.

### 3. Integrations should be transparent, narrow translators

The successful SDKs meet users inside existing libraries. Sentry installs default and auto-enabling integrations during client initialization ([integration resolution](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/client.py#L675-L708)). LangSmith patches OpenAI/Anthropic clients and treats streaming completion as a lifecycle distinct from a normal function return ([stream wrapper](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/run_helpers.py#L829-L899)). Its OpenAI Realtime integration proxies the connection without changing the caller's `async for` loop and keeps tracing errors from escaping into that live loop ([transparent wrapper](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/integrations/openai_realtime/_connection.py#L468-L505), [failure isolation helper](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/_internal/voice/helpers.py#L26-L37)).

The adapter contract for Earshot should be no richer than:

1. identify a conversation and provider session;
2. translate provider callbacks/wire events into canonical boundary events;
3. attach raw provider extensions under a namespaced field;
4. close or abandon the session deterministically;
5. never change provider return values, exceptions, ordering, or cancellation behavior.

Provider SDK classes and payload objects must not cross into Earshot's storage or analysis contracts. They change too quickly. The adapter owns version-specific extraction; the core receives a stable event DTO. For streaming APIs, test early break, consumer cancellation, provider exception, never-consumed generator, double close, and process shutdown. LangSmith explicitly notes that an unexhausted generator can leave a run pending ([generator caveat](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/run_helpers.py#L386-L394)); Earshot should convert this into an explicit `abandoned`/`incomplete` terminal state rather than a permanently open incident.

### 4. Bounded memory is mandatory; silent loss is not acceptable

LangSmith uses a bounded priority queue and drops new items when full, aggregating a rate-limited warning with the number discarded ([drop accounting](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L122-L145), [non-blocking enqueue](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L2392-L2402), [queue-limit regression test](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/test_client.py#L4781-L4832)). OpenTelemetry's batch processor uses a bounded deque, drops the oldest item on overflow, logs a warning, and can emit internal processor metrics ([batch queue](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/_shared_internal/__init__.py#L90-L125), [overflow behavior](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/_shared_internal/__init__.py#L195-L213)). Langfuse gives score and media queues a 100,000-item cap and warns on dropped updates ([queue creation](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/resource_manager.py#L321-L397), [overflow reporting](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/resource_manager.py#L484-L548)).

Sentry has the best model to copy for diagnostics. A failed enqueue records `queue_overflow`; sampling, hooks, network errors, and rate-limit backoff record distinct reasons; later envelopes carry a `client_report` containing reason, category, and quantity ([loss aggregation](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/transport.py#L282-L319), [client-report item](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/transport.py#L422-L452), [queue overflow path](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/transport.py#L694-L706)).

Earshot needs an SDK-local health ledger with monotonically increasing counters at minimum for:

- accepted, exported, retried, rejected-invalid, dropped-overflow, dropped-privacy, dropped-sampled, and abandoned-session;
- queue depth and high-water mark;
- last successful export time;
- last error class/status, without secret-bearing message text;
- oldest pending event age;
- per-provider adapter parse failures and unknown event types.

Expose this through `client.status()` and a callback/logging hook. Where a backend exists, periodically export a compact client health report. A warning alone is insufficient: production users often suppress SDK logs, and “no incidents” must be distinguishable from “instrumentation stopped working.”

Voice state also needs hard caps independent of the transport queue. LangSmith's voice session caps retained audio by duration/bytes, marks the root `audio_truncated`, and catches WAV construction failures so the trace can still close ([bounded audio state](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/_internal/voice/session.py#L52-L88), [truncation handling](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/_internal/voice/session.py#L143-L188), [best-effort finalization](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/_internal/voice/session.py#L404-L448)). Earshot should cap per-channel bytes, session duration, open turns, retained unknown events, transcript characters, and pending media uploads, and must surface every truncation in the evidence record.

### 5. Retry behavior and idempotency are one design decision

OpenTelemetry's OTLP HTTP exporter retries connection failures, HTTP 408, and 5xx responses with exponential jitter up to a total deadline, and its wait is interruptible by shutdown ([HTTP retry loop](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py#L156-L255), [retryable statuses](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/_common/__init__.py#L15-L21)). Its gRPC exporter also honors server `RetryInfo`, reconnects once on `UNAVAILABLE`, and aborts retry on shutdown ([gRPC retry behavior](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/exporter/opentelemetry-exporter-otlp-proto-grpc/src/opentelemetry/exporter/otlp/proto/grpc/exporter.py#L461-L538)). LangSmith's HTTP adapter respects `Retry-After` and allows retries for all methods on supported urllib3 versions ([retry configuration](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L586-L615), [explicit backoff](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L1998-L2020)). Sentry instead emphasizes server rate-limit state and loss accounting, dropping rate-limited categories locally until their window expires ([rate-limit handling](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/transport.py#L322-L351), [local category filtering](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/transport.py#L467-L486)).

None of this alone proves exactly-once delivery. Retrying a whole batch after a response is lost can duplicate accepted events unless the receiver deduplicates them. Earshot should make this explicit:

- assign a stable event ID at event creation, not export time;
- assign a stable batch attempt ID plus an idempotency key derived from ordered event IDs;
- have ingestion enforce uniqueness within project/session scope;
- retry only connection errors, 408, 429, and selected 5xx statuses;
- honor `Retry-After`, apply jitter, cap attempts and total elapsed time;
- never retry validation/auth failures;
- preserve the same IDs and serialized semantic payload across attempts;
- record the terminal reason when a batch is abandoned.

Without those rules, a “reliable” retry layer can corrupt counts and reconstruct a false voice timeline.

### 6. Flush, shutdown, fork, and cancellation are public semantics

OpenTelemetry registers provider shutdown with `atexit`, unregisters it on explicit shutdown, and reinitializes locks/resources after `fork` ([provider lifecycle](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L1309-L1362), [shutdown/flush](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L1475-L1491)). Its batch processor clears the inherited queue and starts a new worker in the child, preventing every forked worker from replaying pre-fork telemetry ([processor fork reinit](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/_shared_internal/__init__.py#L117-L139)).

Langfuse explicitly learned and extended this pattern: after fork it replaces a possibly locked class mutex, recreates internal HTTP connection pools, replaces queues, and restarts non-OTel consumer threads while preserving caller-owned custom clients ([fork registration](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/resource_manager.py#L279-L300), [child reinitialization](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/resource_manager.py#L400-L472), [fork regression tests](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/tests/unit/test_resource_manager.py#L175-L410)). Sentry's worker tracks the PID and starts a new daemon thread when the current process differs, while its tests include real fork cases and deadlock regressions ([PID-aware worker](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/worker.py#L50-L104), [fork test strategy](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/tests/test_metrics.py#L529-L571)).

Earshot must document and test these contracts:

- `flush(timeout)` attempts all data accepted before the call and reports success/failure; a timeout must be real, not ignored downstream;
- `shutdown(timeout)` is idempotent, stops acceptance, drains within the deadline, releases workers/sockets, and unregisters atexit hooks;
- capture after shutdown returns a visible rejection result or increments a counter;
- the child process never replays the parent's pending queue and never reuses inherited connection pools;
- async cancellation never masks the user's cancellation and still marks the session incomplete best-effort;
- serverless users have a documented synchronous/export-now mode or explicit awaitable flush;
- caller-owned transports remain caller-owned and are not silently replaced or closed.

LangSmith's explicit `flush`, `cleanup`, and idempotent `close` are good surface references ([flush](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L3926-L3960), [cleanup/close](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L10063-L10114)), but Earshot should adopt the stronger fork and loss semantics visible in OTel, Langfuse, and Sentry.

### 7. Sampling must preserve a conversation's causal integrity

OpenTelemetry's `TraceIdRatioBased` sampler is deterministic from the trace ID, and `ParentBased` delegates every child decision from its local or remote parent ([ratio sampler](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/trace/sampling.py#L246-L299), [parent sampler](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/trace/sampling.py#L301-L377)). LangSmith stores rejected root trace IDs so children and later patches follow the original decision ([sampling filter](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L2341-L2390), [child-consistency test](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/test_client.py#L2436-L2544)). Sentry's precedence is explicit decision, callback, inherited parent, then configured rate; it validates callback output and applies backpressure downsampling ([sampling precedence](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/tracing.py#L1166-L1249)).

Earshot should sample at the conversation root with a deterministic hash of `(project_id, conversation_id, sampling_salt)`. All causal events, media references, derived turn facts, and incident analyses follow that decision. Provide an explicit override for known failures and a tail-sampling path only if the buffering/privacy costs are acceptable. Never independently sample ASR, LLM, TTS, or transport children: the result looks complete but supports false latency conclusions.

Sampling also must not be confused with media capture. A sampled conversation may still omit audio by privacy policy, and an unsampled conversation may still contribute aggregate SDK health counters. Keep these decisions separate.

### 8. Privacy transformations must fail closed for voice

LangSmith offers whole-field hiding, callbacks, and a curated secret anonymizer applied before sending inputs, outputs, and errors ([client controls](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L932-L1000), [secret anonymizer](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/anonymizer.py#L342-L366)). Its voice helper replaces raw bytes and truncates unexpectedly large strings before attaching event payloads ([voice scrubber](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/_internal/voice/helpers.py#L40-L59)). Langfuse applies user masking to normal observation fields and supports a batch-level OTel masking callback; invalid or throwing batch masks drop the export batch rather than leaking unmasked values ([field masking fallback](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/span.py#L534-L580), [batch mask failure behavior](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/span_exporter.py#L261-L326)). Sentry defaults `send_default_pii` to false and scrubs a security denylist across request, user, frames, breadcrumbs, extras, and span data; custom `before_send` can drop a fully serialized event ([scrubber defaults](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scrubber.py#L63-L89), [scrub surfaces](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scrubber.py#L118-L181), [before-send loss handling](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/client.py#L912-L969)).

Voice raises the stakes because audio and transcripts are biometric/content data, and raw provider events often embed headers, URLs, tool arguments, and base64 chunks. Earshot's stable policy should be:

- audio capture off by default;
- transcript capture separately configurable from audio;
- raw provider payload retention off by default;
- metadata allowlist, not a denylist alone;
- secrets scrubbed in keys, values, URLs, headers, exception text, attachment names, and nested structures;
- custom masking runs before persistence and before queueing/export;
- masking exception means drop/quarantine the affected record, never send original data;
- every record carries the applied privacy-policy version and redaction/truncation flags;
- media uses content hashes and opaque object references, never credential-bearing source URLs;
- deletion semantics cover local queues, durable storage, media blobs, derived artifacts, and correlation indexes.

Do not copy Sentry's `recursive=False` default for generic nested data into a voice payload model ([constructor default](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/sentry_sdk/scrubber.py#L63-L88)). Voice provider payloads are deeply nested by construction.

### 9. Protocol and migration stability should be designed before adoption

OTel's most important engineering choice is separating the instrumentation API from the SDK/exporter. Instrumentation scopes carry name, version, schema URL, and attributes, while the SDK caches tracers by that scope ([scope-aware tracer creation](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L1406-L1462)). That makes “who emitted this and under what schema” part of the data contract. Langfuse reuses OTel as the ingestion seam and tags exports with SDK name/version and public key ([authenticated OTLP processor](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/span_processor.py#L66-L143)). LangSmith supports LangSmith-only, OTel-only, and hybrid modes while preserving deprecated configuration paths ([mode resolution](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L157-L228), [client migration options](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/langsmith/client.py#L1042-L1068)). Langfuse likewise retains explicit legacy API namespaces and deprecates public behavior instead of abruptly deleting it ([legacy/deprecation boundary](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/client.py#L459-L475), [trace-level compatibility method](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/client.py#L1564-L1587)).

Earshot should freeze these v1 layers independently:

1. **Public Python API version**: user imports, client lifecycle, decorators/context managers, adapter entry points.
2. **Canonical event schema version**: immutable voice boundary/event fields and validation semantics.
3. **Transport envelope version**: batching, compression, auth, idempotency, acknowledgements, and client-health reports.
4. **Analysis version**: derived facts/incident logic, which may evolve without rewriting raw evidence.
5. **Adapter identity**: provider, adapter package version, provider SDK version, and mapping revision.

Unknown optional fields must round-trip. Unknown major schemas must be rejected clearly. New analysis must be able to recompute from old canonical evidence. Provider-specific data belongs under namespaced extensions so a provider update does not force a canonical schema bump. Maintain golden fixtures for every released schema and adapter mapping.

### 10. Test architecture is part of the product

The mature projects spend tests on the boundaries that ordinary unit coverage misses:

- LangSmith tests queue overflow/drop warnings, sampling consistency across create/update operations, retry behavior, explicit close/atexit cleanup, generator/async wrapper behavior, and voice adapters ([queue test](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/test_client.py#L4781-L4852), [retry tests](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/test_client.py#L2177-L2280), [close test](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/test_client.py#L6615-L6645), [voice wrapper tests](https://github.com/langchain-ai/langsmith-sdk/blob/b3102211cc66680910b6d64992fece19fb68eadf/python/tests/unit_tests/wrappers/test_openai_realtime.py)).
- Langfuse tests real fork-reinitialization invariants, multi-project separation, concurrent operations, masks, media, and OTel filtering ([fork suite](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/tests/unit/test_resource_manager.py#L175-L410), [multi-project/concurrency suite](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/tests/unit/test_otel.py#L1993-L2350), [mask tests](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/tests/unit/test_mask_otel_spans.py)).
- OTel tests API and SDK separately, includes actual collector functional tests, fork scripts, queue overflow, processor metrics, and exporter behavior ([OTLP functional test](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/tests/opentelemetry-docker-tests/tests/otlpexporter/test_otlp_traces_functional.py), [batch processor tests](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/tests/shared_internal/test_batch_processor.py), [fork regression script](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-sdk/tests/trace/scripts/tracer_provider_resource_after_fork.py)).
- Sentry generates an enormous tox compatibility matrix across Python and multiple old/current integration dependency versions, and its suite contains real fork, queue, rate-limit, async, privacy, and lost-event tests ([matrix source](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/scripts/populate_tox/config.py), [transport tests](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/tests/test_transport.py), [context-variable fork tests](https://github.com/getsentry/sentry-python/blob/3a50950e632d9923f45a9b6fb18d89e1f27badad/tests/utils/test_contextvars.py)).

Earshot's release gate should therefore include more than line coverage:

- supported Python versions, operating systems, sync/async runtimes, and minimum/current provider SDK versions;
- golden provider event fixtures plus a small number of opt-in live capture tests;
- contract tests shared by every adapter;
- a fake collector that exercises acknowledgements, duplicates, 408/429/5xx, invalid auth, slow responses, partial connection failure, and retry-after;
- queue saturation, memory caps, loss reports, shutdown deadlines, atexit, real fork, and serverless one-shot execution;
- context isolation across tasks, threads, nested conversations, and multiple projects;
- malformed/untrusted payload fuzzing, privacy property tests, and URL/secret corpus tests;
- deterministic re-analysis of historical schema fixtures;
- wheel/sdist/container installation tests that import only public APIs from a clean environment;
- compatibility tests proving old adapter packages can talk to the new core and vice versa within the declared range.

## What Earshot should change before the frontend hardens

### Release-blocking SDK and protocol work

1. **Freeze the public kernel.** Keep `Client`, `init/get_client`, a conversation context manager, a low-level canonical event capture method, `flush`, `shutdown`, `status`, and provider `wrap_*` helpers. Mark everything else internal. Do not let the frontend or adapters import storage and analysis internals.
2. **Publish explicit lifecycle semantics.** Define return values and timeout behavior for capture/flush/shutdown, repeated close, post-close capture, async cancellation, fork, and atexit. Add the matrix above before users depend on accidental behavior.
3. **Make telemetry loss observable.** Add a Sentry-like client health/loss report with reasoned counters. The SDK must be able to say “three provider events were rejected and two batches expired,” not merely log an exception.
4. **Specify idempotent ingestion.** Stable event IDs, stable retry payloads, batch idempotency keys, uniqueness constraints, and duplicate acknowledgements are required before enabling automatic retries broadly.
5. **Separate canonical evidence from provider extensions.** Version both, record adapter/provider SDK versions, preserve unknown optional fields, and reject unknown major schemas. The frontend must render canonical facts and evidence quality, not infer semantics from arbitrary provider JSON.
6. **Make privacy policy structural.** Separate audio, transcript, metadata, raw payload, and media-reference controls. Apply recursive fail-closed masking before persistence/export. Store policy version plus redaction/truncation evidence.
7. **Harden conversation sampling.** Deterministic root decision, parent-consistent children, explicit error override, and separate media-capture policy.
8. **Bound every voice-specific accumulator.** Audio bytes/duration, transcript size, open events, unknown event cache, media upload queue, adapter session map, and correlation state. Every cap produces a structured diagnostic.
9. **Add fork and deployment correctness now.** Recreate locks, internal HTTP clients, queues, workers, and process metadata in the child; do not replay parent queues; preserve caller-owned resources. Test Gunicorn preload and uWSGI constraints.
10. **Make the frontend consume an explanation API.** The backend should return canonical timeline boundaries, derived facts, provenance, confidence/availability, truncation/loss flags, and analysis version. Do not make browser code reconstruct causal stages by matching names or inventing contiguous windows.

### Product work that creates the moat

The primary UI object should not be a generic trace. It should answer, with evidence:

- What did the user experience?
- Where did time accrue: capture/VAD, network ingress, ASR, orchestration/LLM/tool, TTS, network egress, playout?
- Was the system speaking when the user interrupted, and what evidence proves it?
- Which measurements are direct, derived, estimated, unavailable, or contradicted?
- Did clock domains align, and what is the uncertainty?
- Was audio/transcript evidence intentionally omitted, truncated, sampled out, or lost?
- Can the same incident be reproduced or compared across providers and releases?

That requires a canonical boundary vocabulary and an evidence graph. A value such as “response latency: 842 ms” is only authoritative if the required start/end boundaries, clock provenance, correlation, and validity checks are present. Otherwise the platform should say “unavailable” or “estimated,” never manufacture a precise number. Generic observability products optimize for recording; Earshot can differentiate by optimizing for **epistemic correctness**.

Build opinionated voice analyses only after the evidence kernel is stable: turn latency decomposition, barge-in/interrupt handling, long silence, ASR finalization delay, tool detours, TTS first-byte/first-audio, playout completion, packet/jitter gaps, cross-talk, provider errors, and cost/quality trade-offs. Each analysis should be versioned, deterministic, recomputable, and accompanied by the exact evidence it used.

### Do not copy these mature-codebase costs

- **Do not create a giant client god object.** The inspected LangSmith client is responsible for tracing transport, prompts, datasets, feedback, sharing, evaluation, auth, cache, and more; Langfuse's client similarly exposes many domain operations. This breadth reflects accumulated product history, not a good starting boundary. Earshot should compose a small capture client from transport, lifecycle, privacy, and adapter modules.
- **Do not use overload volume as domain design.** Langfuse has many overloads and wrapper subclasses for observation types ([observation overloads](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/client.py#L512-L991), [wrapper types](https://github.com/langfuse/langfuse-python/blob/11a232d0eec6e2d25298e7d727aa546b48fd6393/langfuse/_client/span.py#L1266-L1523)). Voice concepts should be data with validators and evidence rules unless behavior truly differs.
- **Do not make the global singleton the only route.** It is convenient but complicates tests, multi-project isolation, embedding, and lifecycle ownership. Keep explicit construction first-class.
- **Do not inherit OTel's set-once global provider restriction as an Earshot API constraint.** OTel intentionally warns and refuses provider replacement ([set-once provider](https://github.com/open-telemetry/opentelemetry-python/blob/1a71171af64a78cc70e61dbe48630644f4b265a8/opentelemetry-api/src/opentelemetry/trace/__init__.py#L544-L566)); Earshot tests and multi-tenant processes need explicit scoped clients even if an OTel bridge uses the global provider.
- **Do not retry non-idempotent batches merely because a mature exporter does.** Add receiver deduplication first.
- **Do not silently discard the oldest causal evidence.** OTel's bounded deque behavior is reasonable for generic telemetry, but losing the conversation start while retaining later derived events can make voice analysis invalid. Prefer dropping/rejecting the whole conversation, or mark it incomplete and prevent authoritative analysis.
- **Do not dump raw provider events for convenience.** LangSmith's current voice helper bounds bytes/strings, but provider payload retention still is not a privacy model. Canonical allowlisted extraction must be the default.
- **Do not promise every integration immediately.** Sentry's compatibility matrix is the cost of pervasive patching. Begin with a few adapters that meet a strict conformance suite and publish their compatibility ranges.

## Final stance

Earshot is directionally differentiated only if it treats voice telemetry as evidence, not as another flavor of spans. The external SDKs confirm that users will expect one-call initialization, transparent integrations, async-safe context, bounded non-blocking export, privacy controls, retries, sampling, and clean shutdown. Those capabilities will not make Earshot unique; defects in any of them will make it untrustworthy.

The opportunity is to make the first open, provider-neutral contract in which a voice diagnosis is auditable: canonical boundaries, clock and correlation provenance, immutable raw evidence, explicit missingness, deterministic analysis, and a UI that shows why a conclusion is true. Freeze that contract and the SDK lifecycle before the frontend encodes assumptions around today's backend. Then add adapters and analyses behind conformance tests. If Earshot does that, its foundation is meaningfully different from a voice-themed LangSmith/Langfuse clone; if it only offers automatic tracing and a timeline, the incumbents are already close enough to absorb the category.
