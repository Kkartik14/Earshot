---
name: product-feature-planning
description: Turn a feature idea into a user-grounded, testable implementation plan by surfacing consequential unknowns, challenging assumptions, researching relevant proven approaches, and defining behavior and acceptance criteria before coding.
---

# Product feature planning

Use this for a new feature or a substantial change before implementation. The job is to make the idea precise enough to deserve implementation: establish the user problem, determine what behavior should exist, expose decisions and risks, and define how success and correctness will be observed.

Stay in planning mode. Do not implement the feature. Separate what the user needs from the solution they proposed, and do not silently decide ambiguous product behavior on their behalf.

## Understand the use case and unknowns

Start from the user's request. Restate the problem and target user in plain language, separating confirmed facts from interpretation. Understand the user's context, current workaround, desired outcome, constraints, and why the proposed solution may be better than simpler options.

Create an unknowns list for decisions that could change the product behavior or implementation. For each, record what is unknown, why it matters, who or what can resolve it, and a sensible default if one exists. Classify each item:

- **Ask the user** about goals, preferences, policy, or workflow choices only they can decide.
- **Inspect or research** factual questions that can be answered from the repository, documentation, or credible external evidence.
- **Propose a default** for low-risk, reversible choices; label it as proposed rather than confirmed.
- **Wait for an explicit answer** on choices that affect security, money, data integrity, irreversible actions, or fundamental product behavior.

Ask focused questions in a small, coherent batch. Explain briefly why each consequential question changes the plan. Avoid making the user answer details that can be established from evidence or handled with a low-risk default. If answers are pending, continue independent research or repository inspection, but do not finalize decisions that depend on those answers.

## Product Architect review

For each non-trivial feature plan, use an available subagent to conduct an independent product review before asking the user about the remaining unknowns. Give it the user's request and relevant context, but withhold your proposed answers and solution where possible so it can surface gaps independently. Ask it to identify:

- The underlying user problem, target user, context, and expected outcome.
- Assumptions, contradictions, missing decisions, and simpler alternatives.
- Important workflows, states, permissions, side effects, and recovery paths.
- Risk-based positive, negative, boundary, and failure scenarios.
- Observable acceptance criteria and plausible success measures.

Brief the subagent with this persona:

> **The Product Architect — Elite Product Manager.** You are a product manager inspired by goal-directed design, rigorous systems thinking, and adversarial quality engineering. Your job is to determine what should be built, why it matters, how it should behave, what could go wrong, and how correctness will be verified before implementation. Separate user needs from proposed solutions. Challenge unnecessary complexity. Think in workflows and states; identify consequential unknowns, credible failure modes, and testable outcomes. Be curious, precise, skeptical, and user-focused. Do not invent requirements or answer user-specific questions for the user. Do not generate edge cases merely to appear thorough; tie each scenario to a requirement or credible risk.

Compare its findings with yours. Reconcile differences using user statements, repository evidence, or cited research. Ask the subagent for another pass if a disagreement could change user-facing behavior, risk, or readiness. Do not repeat reviews just to collect votes. If subagents are unavailable, say so and perform the review directly without claiming independent review occurred.

## Research when it can improve the decision

Research externally when the question is current, specialized, consequential, or likely to benefit from evidence about established product or engineering practice. For examples at larger scale, search engineering blogs and postmortems from companies that have operated similar workflows, along with official documentation and relevant papers where useful.

Prefer first-party sources that describe what was built and the context in which it worked. Compare their users, scale, constraints, and tradeoffs with this feature; adapt lessons instead of copying a design because a large company used it. Cite sources next to the claims they support, distinguish source-backed facts from your inference, and state when the evidence does not settle a product choice. Do not browse just to decorate the plan or to answer questions that are better put to the user.

## Define behavior and derive scenarios

Describe the expected behavior before discussing implementation detail. Include user stories and outcomes, preconditions and postconditions, functional requirements, business rules, validation, error handling, loading/empty/success/failure states, permissions, and out-of-scope behavior where relevant.

Model important workflows as state transitions, not just screens. Identify legal and forbidden transitions, invariants, and side effects. Consider repeated actions, stale state, cancellation, retries, partial failure, concurrent updates, direct API access, and recovery when they are plausible for this feature. Ensure the proposed UX, API behavior, persistence, and business rules agree.

Derive test scenarios during planning and link each scenario to an acceptance criterion or a credible risk. Consider happy paths, valid alternatives, boundaries, invalid actions, state changes, dependency failures, concurrency, permissions, data integrity, and recovery as relevant. Do not mechanically include every category for every feature.

Write acceptance criteria as observable conditions. Use Given–When–Then when it makes the starting state, action, and expected result clearer. Engineers and QA should be able to independently judge whether each criterion passes.

## Use the codebase architecture review when applicable

When the plan will change an existing repository and `pre-feature-architecture-review` is available, invoke it after product behavior is sufficiently clear and before declaring the plan ready. Incorporate its evidence-backed reuse decisions, dependency map, risks, and recommended code flow into the plan. Keep product decisions grounded in the user's needs; use the architecture review to shape how the agreed behavior fits the codebase. Skip this handoff for product-only exploration with no repository implementation target.

## Prioritize risk and define readiness

Assess risk in proportion to user impact, likelihood, financial/security/data-integrity consequences, number of affected users or workflows, and recoverability. Separate release blockers from acceptable limitations and future improvements. Balance value and implementation cost; do not let low-impact edge cases stall a useful feature.

Define success metrics that reflect the user outcome. For each useful metric, state the baseline if known, target or guardrail, and how it will be measured. Identify instrumentation or dependencies needed to measure it. If there is no evidence for a numeric target, leave it as an open decision instead of inventing one.

Mark the plan **Ready for implementation**, **Needs user decision**, or **Needs evidence**. A plan is ready only when critical user-facing decisions are confirmed or explicitly accepted, requirements and acceptance criteria are testable, consequential risks and release blockers are clear, and an implementation path and success measures are understood. Readiness is a planning result; it is not permission to start coding.

## Required planning output

Clearly label each item as **Confirmed**, **Proposed**, **Assumption**, or **Open** where applicable. Produce:

1. Problem statement and target user.
2. User goals and expected outcomes.
3. Proposed solution and rationale, including simpler alternatives considered.
4. Assumptions, constraints, and open questions, with owners and consequences.
5. Functional requirements, business rules, and preconditions/postconditions.
6. User flows and important state transitions.
7. Observable acceptance criteria.
8. Positive, negative, boundary, and failure scenarios tied to criteria or risks.
9. Risk assessment, release blockers, and acceptable limitations.
10. Dependencies, relevant research, instrumentation, and success metrics.
11. Explicit out-of-scope items.
12. Definition of readiness and current status.

End with the unresolved questions that need the user's input and the proposed defaults they can accept or change. Do not claim a feature has been implemented, tested, or verified during this planning workflow.
