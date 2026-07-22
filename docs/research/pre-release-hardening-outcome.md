# Earshot pre-release hardening outcome

Date: 2026-07-22  
Release target: `0.1.0`, experimental `v1alpha1`

This report records the implementation outcome of the first-principles audit. The
original audit remains useful as evidence of the defects that were found; it is not a
description of the current worktree.

## Verdict

Earshot now has a credible pre-v1 engineering foundation for a limited alpha. The
evidence model remains the strongest part of the system, and the SDK, ingest boundary,
viewer, packaging, and migration behavior now meet the basic reliability and truthfulness
standards expected of an observability SDK.

This is not evidence that the product is ready for an unrestricted production launch.
The release should remain explicitly alpha until the CI compatibility matrix passes on a
clean remote runner, real operator-supplied provider deliveries pass the opt-in tests, the
compiled application receives a manual browser/accessibility pass, and at least one
external application completes a soak test with deliberate outages, process exit, fork,
queue pressure, and credential rotation.

The defensible product position is not “the first voice observability platform.” Mature
systems already ship voice integrations. Earshot's differentiated position is an open,
provider-neutral voice evidence contract whose conclusions are auditable: explicit
boundaries, clock provenance, uncertainty and missingness, privacy policy, immutable
artifacts, deterministic analysis, and a backend-authored explanation of every claim.

## Release-blocking defects resolved

- Local trust no longer accepts attacker-controlled Host names, and container build
  context excludes secrets and local research trees.
- Browser credentials are exchanged for bounded HttpOnly sessions; bearer credentials
  are not kept in browser storage, unsafe cookie methods require CSRF, issuer revocation
  invalidates sessions, and mid-session `401` returns the UI to login without retries.
- The SDK has one explicit `Client` implementation behind both explicit and global
  entry points, with deterministic conversation sampling, context isolation, suppression,
  idempotent initialization, active-recorder reconfiguration guards, fork recovery,
  atexit cleanup, public flush/shutdown, and non-secret health status.
- Export endpoints are strictly parsed and credentials are redacted. Async delivery is
  count- and byte-bounded; retry is deadline-limited, interruptible, idempotent, jittered,
  gzip-aware, and honors `Retry-After`.
- Explicit synchronous and durable delivery modes cover serverless and restart-replay
  use cases. Durable records are atomic, private, bounded, checksummed, quarantined on
  corruption, and bound to an endpoint/project destination fingerprint.
- SDK project identity is asserted in `X-Earshot-Project-Id` and checked against the
  authenticated backend project. It cannot silently select another tenant.
- Negative durations/counters and out-of-range probabilities cannot become measured
  truth. Authoring, validation, and analysis share one measurement-policy implementation.
- The viewer consumes a versioned backend explanation. It distinguishes points,
  intervals, and unavailable timing; uses exact integer subtraction before conversion;
  refuses cross-clock placement; and does not invent stage windows or zero durations.
- Turn/stage ordering follows comparable evidence time instead of lexical identifiers.
  Provider measurements bind to an explicit operation, or use a fallback only when
  ownership is unambiguous.
- Storage migrations are transactional and interruption-tested. Existing data with a
  missing correlation key fails closed, and backup/restore requirements are documented.
- The package is honestly pre-v1: contract/protobuf/schema identifiers are `v1alpha1`,
  public versions are centrally defined at `0.1.0`, and the advertised top-level Python
  kernel is small. FastAPI/Uvicorn are isolated behind the `server` extra.
- Clean wheel and source-distribution gates prove generated bindings, viewer assets,
  dependency metadata, base install, server install, CLI, API, and compiled assets from
  the built artifacts rather than the source checkout.
- Reusable conformance gates cover every shipped raw adapter and finalized connector,
  plus real installed LiveKit/Pipecat object surfaces. CI defines Python, framework-min,
  framework-current, Linux, and macOS lanes.

## Engineering mistakes in the original implementation

The central mistake was not the evidence model. It was treating integration edges as
secondary details even though those edges become the permanent SDK contract.

1. Configuration was global state rather than owned runtime state. That made resource
   lifetime, fork behavior, reconfiguration, testing, and multi-project isolation
   accidental.
2. “Accepted by an in-memory queue” was allowed to look like successful delivery. Loss,
   pressure, retry, and shutdown outcomes were not first-class product facts.
3. Security depended on deployment intent rather than the actual request boundary:
   bind host, Host header, Docker context, browser credential storage, and project scope
   were not independently enforced.
4. Version `1.0.0` appeared in contracts while the package was `0.1.0`. That would have
   frozen accidental interfaces and semantics before real users tested them.
5. Backend epistemic discipline stopped at the browser boundary. The frontend recreated
   causal meaning and therefore invented timing that the backend itself would reject.
6. Tests were broad but concentrated on isolated correctness. They did not initially
   exercise clean artifacts, migrations under interruption, fork/exit, real dependency
   objects, restart replay, mid-session auth expiry, or adversarial cross-project routes.
7. Provider/framework version claims were documentation, not a compatibility matrix.
8. Product positioning was too broad. “Voice observability” is already becoming a
   feature of generic platforms; evidence quality and auditable voice causality are the
   narrower moat this code actually supports.

## Remaining gates and limitations

- Run the complete local release command set after the final diff, then require the new
  remote CI matrix to pass from a clean checkout. Local success cannot prove Python
  3.12/3.13, Linux, newest dependency resolution, or the macOS lane.
- The checked-in webhook fixtures are sanitized synthetic deliveries. Do not describe
  them as captured or real; run the existing opt-in tests with operator-supplied payloads
  before claiming provider production compatibility.
- Perform a manual compiled-browser and accessibility pass. The in-app browser surface
  was unavailable during this review, so automated DOM tests, type checking, build, and
  asset smoke are the evidence available here.
- A durable spool root belongs to one client process and is plaintext. It needs a private
  encrypted volume. It is not a coordinated multi-process queue; give workers separate
  roots or use an external durable transport. Durability begins only after final close;
  active recorders are not incrementally journaled and a crash loses the unfinished call.
- There is no generic live OTLP receiver and no clock-alignment layer. The first is a
  product interoperability gap; the second is an intentional truth constraint, so
  unaligned facts remain unavailable rather than guessed.
- Browser sessions and connector rate limits are process-local, and the SQLite backend
  is a single-node alpha architecture. Shared coordination and a multi-node persistence
  design are required before presenting the backend as hosted multi-tenant infrastructure.
- This review proves code paths and synthetic fault behavior, not real-world throughput,
  provider drift, operator ergonomics, or product-market fit. External alpha users and a
  sustained fault-injection soak are still required.

## Launch recommendation

Publish only as a clearly labeled `0.1.0` alpha to a small design-partner group. Freeze
the evidence vocabulary and SDK kernel only after those users validate installation,
instrumentation, privacy defaults, explanations, and recovery behavior. Do not call the
contract v1 or make a broad “first-of-its-kind” claim. Lead with auditable,
provider-neutral voice evidence and the system's refusal to manufacture certainty.
