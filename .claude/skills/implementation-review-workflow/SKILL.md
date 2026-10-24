---
name: implementation-review-workflow
description: Implement substantial repository changes, then run four independent engineering reviewers and two sequential judges; repair evidence-backed findings, re-review changed code, and report the verified release gate.
---

# The Work, the Tribunal, and the Gate

> A patch is a claim. A test is a witness. Review is the cross-examination.

Use this skill when implementing a substantial feature, fixing a consequential defect, or repairing a CI failure. It carries work from a clear request to a finished, evidenced result. For product discovery, use `product-feature-planning`; for a feature whose behavior is already agreed but whose code path is uncertain, use `pre-feature-architecture-review` before implementation.

The tribunal begins after the implementation is complete enough to review as one coherent change. Before then, work carefully: read the instructions that govern the files, know the user’s acceptance criteria, inspect the worktree, and understand the checks the project expects. The judges should meet a finished argument, not a moving target.

## Before the first line

Read the applicable `AGENTS.md` files, repository guidance, and the relevant plan. State the behavior being changed, its boundaries, and the constraints that matter. Inspect the existing worktree and identify the exact base for comparison; include all task-related local edits as well as any pushed changes. Never assume a pull request diff contains the whole story.

Map changed areas to their real verification: tests, generated output and drift checks, formatting, lint, types, builds, packaging, consumer checks, service setup, and supported runtime or platform matrices. Read workflow files and package scripts instead of guessing. For a CI failure, follow the exact run, SHA, job, command, environment, and error; a failure before tests says nothing about gates the job never reached. Preserve workflow order when setup steps establish state or warm dependencies.

For a defect, name the observable failure before repairing it. Use the project’s appropriate test-first workflow when it applies, and consult `test-audit` when adding or changing tests. Keep tests at the layer that owns the behavior. For concurrency, cancellation, and ordering, prefer observable state and synchronization barriers over brittle timing claims.

## Make the change

Keep one implementer responsible for edits in the shared worktree. Let reviewers read and challenge; do not let parallel agents write into the same patch. Make the smallest complete change that satisfies the agreed behavior. Keep policy in the layer that owns it. Add no abstraction, retry, fallback, dependency, comment, or test seam without a reason the code can defend.

Run focused checks as the implementation takes shape. Run generators before their drift checks. When the implementation is ready, run the exact mapped verification set, including the checks CI would reach after the original failure. If the environment cannot run a required check, preserve the result as **unverified** and say why. Do not call a partial run a full pass.

Do not stage, commit, push, publish, deploy, or release unless the user has authorized that action. A tribunal verdict is evidence about the patch; it is not permission to ship it.

## The four executioners

When the implementation is ready, freeze the review scope: provide every reviewer the same requirements, acceptance criteria, comparison base, complete changed paths, relevant workflows, and current revision. Ask four independent subagents to inspect the whole relevant change. Their first passes should be independent; keep their findings from one another until each has made its own read. Each receives a distinct lens. These are review personas, not claims that the agents are the named people or speak for them.

**Linus Torvalds, the Kernel Executioner,** looks for the false premise hiding beneath a clean diff: a broken invariant, a careless interface, an unnecessary layer, or an assumption no caller promised to honor. He asks what the patch does, what it must always preserve, and where the evidence proves it.

**John Carmack, the Performance Executioner,** follows the work through the machine: allocations, latency, throughput, algorithms, and bottlenecks. He rejects waste that has a real cost, and rejects optimization theater when no measurement or credible hot path supports it.

**Fabrice Bellard, the Algorithmic Executioner,** tests the shape of the solution: its complexity, bounds, precision, memory use, and mathematical edges. He searches for the simpler algorithm or tighter representation that remains correct under the full range of inputs.

**Leslie Lamport, the Correctness Executioner,** asks what happens when time and order turn hostile: operations race, messages repeat, state arrives late, a process stops halfway, or recovery begins after partial failure. He demands explicit invariants and evidence proportional to the consequences.

Let each executioner report concrete findings before the debate begins. Then bring the findings into one room. Let them challenge one another’s reasoning, question the proposed repair, and point to the same code, contract, reproduction, or test. They may disagree; they may not substitute volume or confidence for proof. A majority cannot outvote a sound correctness objection.

Every objection must name its severity, file and symbol or line, a realistic scenario, its consequence, and the evidence or reproduction that supports it. Separate confirmed defects from risks, open questions, and taste. A style preference without a material consequence is not a defect. A substantiated objection is not dismissed until it has been fixed or answered with evidence.

The implementer owns the response. Merge duplicate findings, repair the real defects, and explain any rejected claim against the code and contract. After every meaningful fix, run its focused check. Then summon **all four executioners again** against the complete updated change. No earlier pass survives a later edit. Repeat the debate and repair cycle until all four explicitly pass the same revision, with no unresolved material correctness objection.

## The first judge: Martin Fowler, the Architecture Arbiter

Only after the four executioners pass does the first judge enter. Martin Fowler’s review lens follows the shape of the whole design: cohesion, coupling, duplication, boundaries, data access, and the cost of understanding this code six months from now. He asks whether each abstraction earns its place, whether responsibilities sit with the right layer, whether a useful MVC boundary fits, whether interfaces are legible, and whether the patch has smuggled in needless dependencies, redundant comments, speculative machinery, or an N+1 query.

The first judge looks for architecture that serves the behavior, not ceremony that serves itself. If he finds a substantiated issue, return to implementation, run the relevant checks, and convene all four executioners again on the updated patch. Then ask the first judge to review that revision anew. The final judge waits until this gate passes.

## The final judge: Kent Beck, the Release Inquisitor

After the architecture arbiter passes, summon the final judge for a fresh examination. Give Kent Beck’s review lens the user’s requirements, the patch, the tests and their actual results, and the record of earlier findings and dispositions. Let him first form his own view, then challenge whether every engineer’s and the first judge’s claims are supported. He looks for missing acceptance criteria, regressions, untested failure paths, assumptions disguised as facts, and tests that pass without proving the promised behavior.

He asks for focused, reproducible evidence. If he finds a substantiated defect, the patch returns to the tribunal: repair it, run the affected checks, call all four executioners again, and then repeat both judicial reviews in order. Nothing is grandfathered in because it passed yesterday.

## The release gate

The work may be called ready only when there are no known critical defects, no unresolved material correctness objections, all required tests and checks pass on the final revision, and every agreed acceptance criterion has evidence behind it. If an environment, service, platform, or hosted check was unavailable, mark it unverified; do not turn absence of evidence into evidence of success.

Before closing, inspect the complete final diff for accidental files, conflict markers, debug code, secrets, and unintended generated output. Report the revision reviewed; the exact commands and results; what was skipped and why; each reviewer and judge finding with its disposition; the acceptance criteria verified; and the remaining limits. If a material claim cannot be verified, say so plainly and leave the gate open.

The tribunal is not a contest in severity. It is a way to let the patch meet the strongest useful objections while change is still possible. Debate hard, repair carefully, and let the evidence—not the title, the persona, or the number of approving voices—decide whether the work is ready.
