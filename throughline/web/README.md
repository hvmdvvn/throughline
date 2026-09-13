# Throughline web (issue #24)

Next.js App Router frontend under `throughline/web/` (see `_docs/PLAN.md` service layout).
Stack: **Next.js + Tailwind + shadcn/ui + Clerk**, calling the **FastAPI** API with authenticated requests.

## Prerequisites

1. Node.js 20+ (`node -v`)
2. FastAPI stack reachable (Compose or `uvicorn`) at `NEXT_PUBLIC_API_URL`
3. A Clerk application (same tenant the API uses via `CLERK_ISSUER`)

## Setup

```bash
cd throughline/web
cp .env.example .env.local
# Fill NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY and CLERK_SECRET_KEY
npm install
npm run dev
```

Open http://localhost:3000 — sign in, then use the shell nav (Onboarding, Diagnostic report, Pipeline, Needs attention, Settings).

**Onboarding** (`/onboarding`, issue #27) guides OAuth → import → changelog → report with live progress polling and email-when-ready. **Diagnostic report** (`/diagnostic`, issue #25) loads tenancy-scoped data from `GET /reports`, `GET /reports/{id}`, and evidence from `GET /reports/{id}/metrics/{metric_key}/evidence` using the Clerk session JWT. Other shell routes remain placeholders that probe `GET /me`.

Unauthenticated access to shell routes is blocked by Clerk middleware (`src/proxy.ts`).

Ensure the API allows the web origin (default `CORS_ORIGINS=http://localhost:3000`). Set API `WEB_APP_URL=http://localhost:3000` so Atlassian OAuth returns to `/onboarding`.

## Scripts

| Command | Purpose |
|---|---|
| `npm run dev` | Dev server (Turbopack) |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run test:smoke` | Lightweight path/wiring checks |
| `npm run build` | Production build |
| `npm run start` | Serve production build |

## Manual checks

1. Unauthenticated visit to `/diagnostic` redirects to Clerk sign-in.
2. After sign-in, `/diagnostic` lists org reports (or an empty state) and shows rework, scope change, spec quality, and estimation accuracy when a report exists.
3. Clicking a figure opens paginated evidence from the Report API.
4. Remaining placeholder routes still probe FastAPI `/me` when API + Clerk JWKS align.

## Auth note

Provider is **Clerk** (same choice as API issue #8 — not WorkOS). Do not build custom auth.
