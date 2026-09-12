# Backlog audit

Date: 2026-09-12  
Repo: https://github.com/hvmdvvn/throughline  
Sources: `_docs/PLAN.md`, `_docs/TASKS.md`, GitHub issues #1–#68, git history

## Repository state

| Fact | Detail |
|---|---|
| Codebase | None. Only `README.md`, `.gitignore`, `_docs/`, agent workflow docs |
| Git history | Single commit: plan + task backlog seed |
| Open issues | 68 (all open, none closed) |
| Complete issues | None |
| Groomed issues | None (no Goal/AC/Out of scope/Constraints template yet) |
| Duplicates | None among #1–#68 |
| Missing referenced doc | `product-definition.md` (cited by plan; not in repo) |

## Stack decision (resolved 2026-09-12)

**Chosen:** align backlog to `_docs/PLAN.md` — **FastAPI + arq + SQLAlchemy/Alembic**. No Django, no Celery.

`_docs/TASKS.md` and GitHub issues rewritten accordingly. Foundation issues unblocked for grooming.

## Issue inventory by phase

| Label | Issues | Count | Status |
|---|---|---|---|
| foundation | #1–#9 | 9 | Open; blocked by stack conflict |
| phase-0 | #10–#27 | 18 | Open; depends on foundation |
| phase-1 | #28–#61 | 34 | Open; depends on Phase 0 + validation gate (#26) |
| phase-2 | #62–#68 | 7 | Open; depends on Phase 1 |

## Dependency sketch

```text
1 → 2 → 3,4,5
1 → 6 → 7
6 → 8
2 → 9
foundation → 10 → 11 → 12 → 13 → 14 → 15
13/15 → 16
14/15 → 17 → 18,19,20,21
17–21 → 22 → 23
8 → 24 → 25
16 + analytics → 26 (go/no-go before Phase 1)
10–25 → 27
Phase 0 + #26 pass → Phase 1 (#28+)
Phase 1 → Phase 2 (#62+)
```

## Vague / needs grooming notes

All 68 issues currently have Goal + Description only. None have checkable acceptance criteria, out of scope, or constraints. During grooming:

- External credentials (Atlassian OAuth app, Slack app, Clerk/WorkOS, LLM keys) should be constrained as “developer provides secrets; code consumes env” — not “register the production app” as AC unless intentional.
- #16 / #26 require verifying public Jira terms of use before relying on a corpus.
- #8: Clerk vs WorkOS is an open choice in plan; grooming must pick one or make it a constraint decision.
- #28: Anthropic vs OpenAI — plan allows either; pick one for first provider.
- Soft-delete rule in plan vs hard-delete in #68: both intended — soft delete day-to-day; #68 is verified hard-delete for retention/policy.

## Conflicts with plan (besides stack)

| Topic | Plan | Issues/tasks |
|---|---|---|
| Soft delete | “nothing is ever hard-deleted (soft delete + retention job)” | #68 requires verified hard-delete path — compatible if hard-delete is the retention/policy path |
| Service layout | `throughline/{api,domain,...}` | Tasks assume Django project + `core` app |
| Workers | arq | Celery throughout Phase 0/1 wording |
| Ops UI | Not Django-centric | Many issues assume Django admin |
| Phase 3 | SSO, SOC2, etc. weeks 23+ | No issues yet for Phase 3 (OK — out of current backlog) |

## What should be groomed first

After stack resolution:

1. #1 (project skeleton) — unblocks everything  
2. #2 (Docker Compose)  
3. #3, #4, #5, #6 in dependency-safe order  
4. Then #7, #8, #9  
5. Then Phase 0 in numeric order, respecting the dependency sketch  

Do **not** implement during grooming.

## Split / follow-up candidates (defer until grooming)

- #1 currently bundles “create repository” (already done) with Django skeleton — trim during grooming.
- #8 may need a follow-up for SSO once provider chosen.
- #26 is a research/write-up gate, not pure eng — keep as issue but AC should include written findings artifact.
- Phase 3 work from plan has no issues yet — create later when Phase 2 completes; do not invent now.

## Orchestrator status

1. Context docs created (`AGENTS.md`, process, roles, template, `CLAUDE.md`).
2. Backlog audited (this file).
3. **Paused:** grooming and implementation blocked on stack decision (FastAPI+arq vs Django+Celery).
