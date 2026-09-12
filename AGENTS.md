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

Requires Python 3.12+. Create and activate a virtualenv first (`python -m venv .venv`) for host-side work.

| Action | Command |
|---|---|
| Install dependencies (host) | `pip install -e ".[dev]"` |
| Start stack | `docker compose up --build` |
| Stop stack | `docker compose down` |
| API health | `http://localhost:8000/health` |
| Prove API → DB/Redis | `docker compose exec api python -m throughline.connectivity` |
| Run migrations | `docker compose run --rm api alembic upgrade head` |
| Run full test suite (container) | `docker compose run --rm api pytest` |
| Run full test suite (host) | `pytest` |
| Run individual tests | `pytest tests/test_app.py` (or any path/node id) |
| Run API only (host) | `uvicorn throughline.api.app:app --reload` |
| Run arq worker (host) | `arq throughline.workers.settings.WorkerSettings` |
| Job status (admin, auth) | `GET /admin/jobs/{job_id}` |
| Jira issue history import (admin, auth) | `POST /admin/jira/import` (enqueue arq job); `GET /admin/jira/import` (progress) |
| Jira changelog import (admin, auth) | `POST /admin/jira/changelog` (enqueue arq job); `GET /admin/jira/changelog` (progress) |
| Diagnostic report (arq) | `generate_diagnostic_report` job — args `org_id`, `range_start`, `range_end` (ISO dates); HTTP surface is issue #23 |
| Public Jira corpus (local/dev) | `python -m throughline.ingest.corpus` (fixture subset); optional `--remote` (manual ASF fetch). ToS: `_docs/public-jira-corpus.md` |
| Lint | `ruff check .` |
| Apply migrations (host, needs Postgres) | `alembic upgrade head` |
| Migration drift check (host, needs Postgres) | `alembic check` |

Copy `.env.example` to `.env` for local overrides (optional; Compose has safe defaults). Set `ENVIRONMENT=production` only with real `DATABASE_URL` / `REDIS_URL` — production refuses to boot if those are unset. Never commit secrets.

After changing the Postgres image (e.g. to enable pgvector), recreate the `db` volume if an older Postgres data dir remains: `docker compose down -v` then `docker compose up --build`.

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
| [`_docs/backlog-audit.md`](_docs/backlog-audit.md) | Backlog audit findings |
| [`_docs/public-jira-corpus.md`](_docs/public-jira-corpus.md) | Public Jira corpus ToS verification (issue #16) |
| `README.md` | Human-facing project overview |

Missing but referenced by plan: `product-definition.md` (what/why). Do not invent it.
