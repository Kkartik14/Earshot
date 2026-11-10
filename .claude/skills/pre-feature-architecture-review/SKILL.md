---
name: pre-feature-architecture-review
description: Map reusable code, dependencies, and risks before planning or implementing a substantial feature; compare reuse options and settle on an evidence-backed end-to-end flow with an independent backend/runtime review.
---

# Pre-feature architecture review

Use this during feature discovery or planning, before implementation, when the change may touch existing flows, shared code, data ownership, APIs, or runtime behavior. The goal is to choose the safest and simplest complete flow the codebase supports, and to make relevant reuse opportunities and risks visible before code is written.

Keep this a planning review. Do not change production code. Read applicable `AGENTS.md` files and project documentation first. Treat the feature request as the source of scope; record assumptions and open questions rather than silently inventing requirements.

## Map the current system

Explore the code paths related to the feature, following behavior rather than relying on name matches alone. Inspect relevant entry points, analogous flows, helpers and primitives, callers, tests, schemas, configuration, external dependencies, and observability. Trace important data and control flow through the layers the feature would touch. Check lifecycle and ownership, error handling, retries, cancellation, transactions, and async or concurrency boundaries where applicable.

For each plausible reuse candidate, record:

- File and symbol, with links or line references when available.
- What it does and where it is called from.
- Its dependencies, contracts, side effects, lifecycle, and important invariants.
- How it matches the feature, what would need to change, and the risks of reuse.
- Evidence checked, including tests or callers that establish its behavior.

Map how relevant candidates depend on or call one another. Use a compact dependency or sequence diagram when it makes the proposed flow easier to verify. Scope the search to the feature's relevant boundaries; state what areas were inspected and do not claim the entire repository was exhaustively searched if it was not.

## Compare reuse options

Give each plausible candidate a transparent reuse-fit score from 0 to 10. Score each dimension 0, 1, or 2, and explain scores with repository evidence:

1. **Behavior fit:** Does it implement the semantics the feature needs?
2. **Contract and lifecycle fit:** Do its API, data ownership, and lifecycle match?
3. **Dependency fit:** Does it fit the affected architecture and runtime boundaries?
4. **Change cost:** Can it be reused with a small, maintainable extension or adapter?
5. **Verification and operations:** Can its behavior be adequately tested, observed, and supported?

Use the score to compare candidates, not as an automatic decision. Label each candidate **reuse**, **adapt**, **reuse the pattern only**, **new implementation**, or **reject**. Record risk separately as low, medium, or high, along with confidence. A material contract or invariant mismatch, data integrity or security concern, unsafe concurrency behavior, or unacceptable operational cost can rule out reuse regardless of score. Explain any such override.

When relevant, choose measurable success metrics for the proposed feature flow. State a baseline, target or guardrail, and how each will be measured. Examples include p95 latency, allocations, throughput, error or retry rate, query count, or resource usage. Do not invent a target without evidence; mark it as a decision to make if no baseline or requirement exists. Keep these outcome metrics distinct from the reuse-fit score.

## Get an independent challenge

Use the available subagent mechanism to launch at least one reviewer for each substantial feature review. Give the reviewer the feature requirements and enough repository context to inspect the relevant code independently. Ask it to find missed candidates, trace dependencies, and make its own reuse/adapt/new/reject calls. Do not give it your scores or verdicts in the first pass; this reduces anchoring.

Give the reviewer the persona the user requested: a distinguished engineer at Microsoft in the David Fowler archetype, the “final boss of backend engineering.” They are a runtime whisperer who sees through abstractions; an abstraction assassin who removes unnecessary complexity; a performance predator who thinks in allocations and latency; a concurrency expert who understands async at its core; and a debugging machine who hunts root causes instead of patching symptoms. They are also a framework architect who builds primitives that empower thousands of engineers and reasons about what the machine is actually doing.

Have the reviewer bring that persona to the technical critique: be direct, challenge assumptions, trace concrete runtime behavior, and back every reuse or design call with evidence. This is an assigned reviewer persona, not a claim that the subagent is David Fowler or speaks for Microsoft.

Compare the independent findings against your map. Discuss each material disagreement with the reviewer using concrete code evidence. Ask for another pass when a disagreement could change the flow, when new evidence appears, or when there may be a significant missed dependency. Stop when the key calls converge or remaining uncertainty is explicit and bounded. Do not repeat reviews just to collect votes. If subagents are unavailable, say so clearly and label the recommendation provisional; do not simulate an independent review.

## Finalize the feature flow

Present a recommended end-to-end flow with each important step marked as reuse, adapt, pattern-only, or new. Include:

1. **Scope and assumptions** — the behavior being planned and unresolved requirements.
2. **Recommended flow** — an ordered path through files, symbols, and data/dependency boundaries.
3. **Candidate decisions** — a table of every relevant candidate found, evidence, score, risk, and final call.
4. **Dependency and impact map** — affected callers, services, data stores, contracts, and async/concurrency boundaries.
5. **Independent review** — what the subagent added or challenged, how disagreements were resolved, and any remaining dissent.
6. **Risks and checks** — mitigations, tests or measurements to perform during implementation, and outcome metrics with known baselines and targets.
7. **Implementation sequence** — a concise order of work that preserves the recommended flow.

End with a clear decision on what to reuse, extend, implement new, or leave out, and why. If evidence is insufficient to make a safe call, identify the exact question or inspection needed instead of presenting a guess as settled.
