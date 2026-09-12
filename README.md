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

## Docker Compose (recommended)

Starts API (`api`), arq `worker`, Postgres 16 + pgvector (`db`), and Redis (`redis`).

```bash
# Optional: local overrides (file is gitignored)
cp .env.example .env

docker compose up --build
```

- API: http://localhost:8000/health
- Prove API → Postgres/Redis from inside the API container:

```bash
docker compose exec api python -m throughline.connectivity
```

That command prints `ok` when both TCP checks succeed (same check used by the `api` service healthcheck).

Apply database migrations (creates the `vector` extension and schema):

```bash
docker compose run --rm api alembic upgrade head
```

Stop with `Ctrl+C` or `docker compose down`.

If you previously ran Compose with plain `postgres:16`, wipe the volume once so the pgvector image can initialize cleanly: `docker compose down -v`.

Postgres and Redis are reachable on the Compose network (`db`, `redis`) and are not published to the host by default (avoids clashing with a local Postgres). Uncomment the `ports` entries in `docker-compose.yml` if you need host access.

### Tests inside the container

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api pytest
```

The pgvector round-trip test (`tests/test_pgvector.py`) applies migrations itself when Postgres is reachable; running `alembic upgrade head` first is still the documented happy path.

## Local setup (without Docker)

Requires Python 3.12+.

```bash
python -m venv .venv
```

Activate the virtualenv, then install the package with dev dependencies:

```bash
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

Run the API locally (expects Postgres/Redis only if you exercise connectivity):

```bash
uvicorn throughline.api.app:app --reload
```

Run the arq worker (requires Redis):

```bash
arq throughline.workers.settings.WorkerSettings
```

### Tests locally

```bash
pytest
```

## Environment variables

Documented in [`.env.example`](.env.example). Do not commit real secrets; `.env` is gitignored.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres URL (Compose default uses host `db`) |
| `REDIS_URL` | Redis URL (Compose default uses host `redis`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres bootstrap |
| `APP_NAME` / `DEBUG` | App settings |

## Application package

The FastAPI app lives under `throughline/api/`. Settings load from environment variables (with safe defaults) via `throughline.config`. The arq worker settings live under `throughline/workers/`. SQLAlchemy models and session helpers live under `throughline/db/`; schema changes are Alembic migrations under `alembic/versions/` (no Django).
