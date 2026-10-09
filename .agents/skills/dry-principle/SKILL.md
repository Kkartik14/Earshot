---
name: dry-principle
description: Apply DRY to code, architecture, data, APIs, and documentation by keeping each rule or behavior authoritative in one clear place.
---

# DRY: one authoritative source per fact

Treat DRY as **each piece of knowledge has one clear authoritative home**, not just avoiding repeated lines of code. Minimize sources of truth so a policy or behavior has one owner and changes do not need to be coordinated across duplicate implementations.

## Apply DRY

- Identify the fact, policy, state, or behavior being changed and name its owner before implementing it.
- Keep domain rules and lifecycle behavior in the service or package that owns that domain. Other layers should use its API or package instead of rebuilding the same behavior.
- Keep boundary checks that protect each component: validate inputs and protocols, authenticate requests, verify delegated scope, and enforce resource ownership. These checks do not require duplicating the upstream business policy. Distinguish the system that makes an authorization decision from the system that verifies and enforces the delegated authority.
- Reuse a shared implementation only when the behavior and its change lifecycle are truly the same. Similar-looking rules can belong to different domains; avoid abstractions that couple services or make one layer depend on another layer's storage.
- Treat caches, projections, generated files, and read models as derived data. Keep the canonical owner clear and define how derived copies are refreshed, invalidated, or rebuilt.
- When deliberate duplication is needed for performance, resilience, or independent operation, state why, identify the authoritative source, and specify how divergence is detected or corrected.

For example, when a runtime service owns call lifecycle, its host should use the service contract rather than independently implementing provider sessions, state transitions, retries, and durable call state. The host still owns its own user session and request boundary; it should pass authorized context to the runtime service rather than copy its call policy.

Before adding a second implementation, ask: **Is this the same knowledge or only similar code? Who owns the rule? Can this layer call or consume the owner's contract? If this copy is derived, how does it stay current?**
