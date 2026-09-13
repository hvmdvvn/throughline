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

Open http://localhost:3000 — sign in, then use the shell nav (Diagnostic report, Pipeline, Needs attention, Settings). Placeholder pages probe `GET /me` on FastAPI with the Clerk session token.

Unauthenticated access to shell routes is blocked by Clerk middleware (`src/proxy.ts`).

Ensure the API allows the web origin (default `CORS_ORIGINS=http://localhost:3000`).

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
2. After sign-in, shell nav links resolve to four placeholder routes.
3. Placeholder shows FastAPI `/me` probe status when API + Clerk JWKS align.

## Auth note

Provider is **Clerk** (same choice as API issue #8 — not WorkOS). Do not build custom auth.
