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

## Stack (from plan)

FastAPI (Python 3.12), arq + Redis, Postgres 16 + pgvector, Next.js, Docker.

**Note:** `_docs/TASKS.md` and some issues still say Django + Celery. That conflict is documented in `_docs/backlog-audit.md` and must be resolved before foundation implementation.
