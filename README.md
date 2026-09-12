# Throughline

AI-assisted requirements and delivery workflow for product teams. Connects Jira history to diagnostic analytics and a PM review loop for specs, clarifications, and change watch.

## Docs

| Doc | Role |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Agent entry point (commands, rules, doc index) |
| [`_docs/PLAN.md`](_docs/PLAN.md) | Product / architecture source of truth |
| [`_docs/process.md`](_docs/process.md) | PM → Engineer → QA workflow |
| [`_docs/TASKS.md`](_docs/TASKS.md) | Historical backlog seed |
| [`_docs/backlog-audit.md`](_docs/backlog-audit.md) | Backlog audit |

GitHub issues are the live units of work: https://github.com/hvmdvvn/throughline/issues

## Stack

FastAPI (Python 3.12), arq + Redis, Postgres 16 + pgvector, SQLAlchemy + Alembic, Next.js, Docker.

Backlog and GitHub issues are aligned to `_docs/PLAN.md` (decision 2026-09-12).
