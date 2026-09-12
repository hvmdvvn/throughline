# Process

How work is performed on Throughline.

## Workflow

```text
Backlog
   ↓
PM grooming
   ↓
Engineer implementation
   ↓
QA verification
   ↓
PASS ───────────────→ Close issue
   │
   FAIL
   ↓
Engineer fixes
   ↓
QA verifies again
```

## Work unit

* GitHub issues are the units of work.
* Work on one issue at a time.
* Never implement an issue that has not been properly groomed.
* Read the issue's acceptance criteria before implementation.
* Re-read the acceptance criteria before considering implementation complete.

A groomed issue uses `_docs/task-template.md` and includes Goal, Acceptance criteria, Out of scope, and Constraints.

Mark groomed issues with the `groomed` label.

## Roles

### PM

Grooms an issue before implementation.

Follows: `_docs/team/pm.md`

### Engineer

Implements one groomed issue.

Follows: `_docs/team/software-engineer.md`

### QA

Tests the implementation against the issue's acceptance criteria.

Follows: `_docs/team/qa-engineer.md`

## Orchestrator

The main agent/session acts as the orchestrator.

The orchestrator coordinates the other agents but does not perform the PM, Engineer, or QA role itself.

## Lifecycle

For every issue:

1. Select the next appropriate open issue.
2. PM grooms the issue.
3. Review the resulting specification for consistency with `_docs/PLAN.md`.
4. Engineer implements it.
5. Engineer writes appropriate tests.
6. Engineer commits the work.
7. QA independently verifies the result.
8. If QA returns `FAIL`, send the issue back to the Engineer with the QA findings.
9. Engineer fixes the problems.
10. Engineer commits the fix.
11. QA runs again.
12. Continue until QA returns `PASS`.
13. Only the orchestrator may close the issue.
14. Move to the next issue.
15. Continue until the backlog satisfies the defined completion condition.

## Selection order

Prefer lowest issue number among open, eligible issues whose dependencies are satisfied.

Foundation (1–9) before Phase 0 (10–27) before Phase 1 (28–61) before Phase 2 (62–68), unless an issue is blocked.

If blocked by a dependency or unresolved conflict with `_docs/PLAN.md`, document the blocker on the issue and move to the next eligible task.

## Completion condition

```text
All eligible backlog issues have been implemented,
QA has returned PASS for each one,
and the corresponding issues have been closed.
```

Eligible means: open, groomed, dependencies met, no unresolved contradiction with `_docs/PLAN.md`.

## Rules

* Do not skip grooming.
* Do not silently change acceptance criteria.
* Do not close issues from the Engineer role.
* QA must not modify production code.
* QA must produce a clear `PASS` or `FAIL`.
* A `FAIL` must explain what was tested and what happened.
* Do not mark an issue complete merely because tests pass.
* Acceptance criteria and running behavior are the final authority.
* Preserve the project's existing architecture and conventions unless the issue explicitly requires a change.
* Make small, coherent commits.
* Never implement unrelated improvements just because they appear useful.
* If additional work is discovered, create or reference a follow-up GitHub issue instead of silently expanding scope.
* If an issue contradicts `_docs/PLAN.md`, stop and report — do not silently choose.
