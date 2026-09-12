# AGENTS.md

Persistent entry point for coding agents working on Throughline.

## Project

Throughline is an AI-assisted requirements and delivery product for PMs. It connects Jira history to diagnostic analytics (Phase 0), a PM review loop that turns intake into approved, traceable specs in Jira (Phase 1), and live change watch on in-flight work (Phase 2).

Constraint from `_docs/PLAN.md`: one engineer, under six months of runway, no design partners yet. Phase 0 is sellable as a paid diagnostic audit. Validation gate at end of week 3: churn signal must be real before Phase 1.

**Source of truth hierarchy**

1. `_docs/PLAN.md` — product direction and architecture
2. GitHub issues — units of work
3. Groomed issue body (task template) — precise implementation requirements

If an issue contradicts `_docs/PLAN.md`, stop and report the contradiction. Do not silently choose.

## Commands

No application code exists yet (repo is docs + backlog only). Commands will be established by foundation issues and recorded here when they exist.

Until then, expected shape from `_docs/PLAN.md` / backlog (not yet runnable):

| Action | Command (TBD) |
|---|---|
| Install dependencies | TBD after project skeleton |
| Run application | TBD (`docker compose up` once issue 2 lands) |
| Run full test suite | TBD (pytest once issue 1 lands) |
| Run individual tests | TBD |
| Lint | TBD (ruff once CI lands) |
| Format | TBD |
| Build | TBD |

Update this section whenever foundation work adds real commands.

## Rules

Derived from `_docs/PLAN.md` and existing repo decisions only:

- Single deployable monolith + worker. Do not build microservices.
- Connector interface boundary is strict (Jira first; Linear/Azure DevOps later). Keep Jira specifics out of canonical models.
- Tenancy: every table except `orgs` carries `org_id`; enforce in one place, not per query.
- Soft delete + retention jobs; no casual hard deletes. Hard-delete path is explicit and tested (issue 68).
- `embeddings.project_id` is always non-null; project filter is required on retrieval, never optional.
- Pipeline emits `diffs`, never direct writes to requirement nodes.
- Do not build: task board with ticket status, Gantt/burndown, agent frameworks/dynamic delegation, meeting recording bot, two-way Jira sync before a customer demands it, per-org fine-tuning, separate graph DB, own auth.
- Auth is hosted (Clerk or WorkOS). Do not build auth.
- Graph queries: recursive CTEs first, not Apache AGE.
- Everything containerized from commit one.
- Secrets never committed; document required env vars.
- Work one GitHub issue at a time through PM → Engineer → QA. See `_docs/process.md`.
- **Stack decision (2026-09-12):** FastAPI + arq + Redis + SQLAlchemy/Alembic. Backlog aligned to `_docs/PLAN.md`. Do not introduce Django or Celery.

## Documents

| Path | Purpose |
|---|---|
| `_docs/PLAN.md` | Product/architecture source of truth |
| `_docs/TASKS.md` | Historical backlog seed (issues are live SoT for work) |
| `_docs/process.md` | PM → Engineer → QA workflow |
| `_docs/task-template.md` | Required shape of a groomed issue |
| `_docs/team/pm.md` | PM grooming role |
| `_docs/team/software-engineer.md` | Engineer implementation role |
| `_docs/team/qa-engineer.md` | QA verification role |
| `_docs/backlog-audit.md` | Backlog audit findings |
| `README.md` | Human-facing project overview |

Missing but referenced by plan: `product-definition.md` (what/why). Do not invent it.
