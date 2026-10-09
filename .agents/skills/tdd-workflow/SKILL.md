---
name: tdd-workflow
description: Drives new backend logic or bug fixes through red-green-refactor — write a failing Jest test first, make it pass minimally, then refactor — instead of writing implementation and tests together or skipping tests. Use when implementing a new service/repository method, fixing a bug, or asked to add test coverage.
---

# TDD Workflow (Jest)

Swish Core runs unit tests via `npm test` and e2e via `npm run test:e2e` (config in `test/jest-e2e.json`). Follow red-green-refactor rather than writing the implementation and tests in the same pass.

## Red
- Write the test first, against the behavior you're about to add or the bug you're about to fix. For a bug fix, the failing test must fail for the *actual* reported reason (see [[systematic-debugging]]) — not an approximation of it.
- Run it and confirm it fails for the expected reason (not a typo, missing import, or wrong mock) before writing any implementation.
- Mock external boundaries (HTTP clients, `readerConnection`/`Repository<T>`, BullMQ queue `.add()`, Winston logger) — do not mock the unit under test itself.

## Green
- Write the minimum code to pass the new test. Resist adding unrelated handling, extra parameters, or generalization the test doesn't require yet.
- Re-run the full file's test suite, not just the new test — confirm no existing case regressed.

## Refactor
- With the test green, clean up: extract guard clauses per ER1–ER4 (`.claude/rules/patterns.md`), remove duplication (check `src/shared/shared.utils.ts` / `src/common/` before introducing a new helper), tighten types (`Promise<any>` → concrete type).
- Re-run tests after every refactor step — the green bar must stay green throughout, not just at the end.

## What needs a test in this codebase
- New/changed service methods with business logic (especially anything touching `SwishResponse`, error codes, or cart/order/payment calculations).
- New BullMQ processor branches (`switch (job.name)` cases) — including the `default` case and the `'failed'` event handler.
- New DTO validation rules — a test that an invalid payload is rejected by `ValidationPipe`, not just that a valid one passes.
- Bug fixes, always — a fix without a regression test is not considered complete.

## What doesn't need a new test
- Pure refactors with no behavioral change (existing tests already cover you — if they don't, that's a coverage gap worth flagging separately, not blocking this change).
- Straightforward CRUD passthroughs already covered by existing e2e tests for that resource.
