# PLAN.md — Throughline

Full build plan. Companion to `product-definition.md`, which covers *what* and *why*; this covers *how* and *in what order*.

**Constraint that shapes everything below:** one engineer, full-time, under six months of runway, no design partners yet. The plan is written as the complete build, but Phase 0 is structured so it can be sold as a standalone paid audit if runway forces the issue. That gate is at week 5.

---

## 1. Stack

Chosen for build speed with one person, and for the LLM ecosystem being Python-first.

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI (Python 3.12) | Fastest path from LLM code to HTTP; Pydantic gives you schema-constrained model output for free |
| Workers | arq + Redis | Async, small, no Celery ceremony. Imports and pipelines are long-running jobs |
| DB | Postgres 16 + pgvector | Single datastore. Relational core, vectors in the same transaction |
| Graph | Recursive CTEs, **not** Apache AGE at first | AGE isn't available on RDS/Aurora/Cloud SQL. At a few thousand nodes per org, CTEs answer every query you have. Revisit only if a real query gets ugly |
| Frontend | Next.js (App Router) + Tailwind + shadcn/ui | Component velocity matters more than anything else here |
| Auth | Clerk or WorkOS | Do not build auth. WorkOS if SSO is on the near roadmap |
| LLM | Anthropic or OpenAI via a thin internal interface | Frontier for generation and critique; see §6 |
| Local LLM | Ollama on the 4060, dev-time only | Classification, extraction, redaction, embeddings. Not generation |
| Hosting | Docker; Hetzner or Fly.io to start, AWS when a customer demands it | Margin matters at $250/seat |
| Tracing | Langfuse (self-hosted) or OpenTelemetry + Postgres | You need per-run traces from day one, not month six |

Everything containerized from commit one, because the self-hosted enterprise tier is the endgame and retrofitting that is brutal.

---

## 2. Service layout

Single deployable monolith plus a worker. Do not build microservices.

```
throughline/
  api/           FastAPI app — routes, auth, tenancy middleware
  domain/        Core models and business rules, no I/O
  pipelines/     The AI graph: extract, classify, decompose, critique, estimate, impact
  connectors/    jira/, slack/, github/ — each one isolated behind an interface
  ingest/        History import, normalization, backfill jobs
  analytics/     Churn, cycle time, estimation accuracy computations
  memory/        Convention profile, retrieval, permission filtering
  workers/       arq task definitions
  evals/         Golden sets, harness, scoring
  web/           Next.js frontend
```

The connector interface boundary is the one piece of architecture worth being strict about. Jira is first, but Linear and Azure DevOps are on the long-term list, and a leaky Jira abstraction will cost you a month later.

---

## 3. Data model

Core tables. Every table except `orgs` carries `org_id` and every query goes through a tenancy-scoped session — enforce it in a single place, not per-query.

**Identity and tenancy**
- `orgs`, `users`, `memberships`
- `projects` — mirrors their Jira project structure
- `project_permissions` — synced from Jira, the basis for retrieval filtering

**Intake**
- `requests` — raw inbound: source (slack/paste/upload/transcript/email), author, raw text, channel refs
- `request_classifications` — bug / duplicate / too-small / needs-spec, with confidence
- `clarifications` — question text, sent-at, answered-at, answer text, source thread ref

**Requirements**
- `requirement_nodes` — id, org_id, project_id, parent_id, type (epic|story|ac), title, body, state, version
- `node_links` — typed edges: `derived_from` (request line), `implements`, `conflicts_with`, `supersedes`
- `node_versions` — full history, because every change is a diff
- `diffs` — proposed change, author (ai|human), rationale, evidence refs, status (pending|accepted|rejected), decided_by, decided_at

**External**
- `jira_issues` — issue key, node_id, status, transitions, sync state
- `jira_events` — raw webhook payloads, append-only
- `github_activity` — PR/commit metadata linked to issue keys
- `sync_state` — per-connector cursors, last-seen, backoff state

**Intelligence**
- `embeddings` — pgvector, with `project_id` on the row so filtering happens in the query, not after
- `conventions` — rule text, scope (org|project), tier (formatting|semantic), status, version, learned_from
- `glossary_terms`
- `estimates` — node_id, range_low, range_high, comparables (node ids), accepted_value
- `outcomes` — cycle time, reopen count, scope-added count, per node

**Governance**
- `audit_log` — every AI proposal, every human decision, immutable
- `runs` — one row per pipeline execution: inputs, retrieved context ids, model, tokens, cost, latency, output

Two rules worth writing down now because they're expensive to add later: nothing is ever hard-deleted (soft delete + retention job), and `embeddings.project_id` is non-null always.

---

## 4. The AI pipeline

A deterministic graph. Fixed nodes, fixed order, retries per node, a trace row per run.

```
classify ──> [reject / small-path / full-path]
                              │
extract ──> decompose ──> critique ──> estimate ──> assemble diff
```

- **classify** — bug/duplicate/too-small/needs-spec. Small model. Duplicate detection is a vector search against existing nodes, not a model judgment.
- **extract** — pull entities, actors, constraints, and explicit asks from raw text. Small model, schema-constrained via Pydantic.
- **decompose** — frontier model. Context: the request, retrieved similar past specs (permission-filtered), the convention profile, glossary, and repo module names. Outputs the epic → story → AC tree.
- **critique** — separate frontier call, adversarial prompt: what's ambiguous, what's untestable, what edge case is missing, what conflicts with an existing approved spec. Produces flags with severity.
- **estimate** — retrieval over `outcomes` for similar past stories, returns a range plus the comparable node ids. This is a search problem with a model wrapper, not a model problem.
- **assemble** — everything becomes a `diff`, never a direct write.

Every node records inputs, retrieved ids, model, and cost. Evidence shown in the UI comes straight from these rows — the trace *is* the evidence feature, which is why it's built first, not last.

**No dynamic delegation.** Add it only where an eval shows it beats the fixed path.

---

## 5. Connectors

### Jira (deepest, hardest, first)
- OAuth 2.0 3LO, offline_access for refresh
- **History import:** `/search` with JQL pagination plus `/issue/{key}/changelog` for transitions. Expect 100k+ issues on a real instance. Backfill as a resumable job with cursors — it will fail mid-run and must not restart from zero.
- **Rate limits:** budget for aggressive backoff. Import speed is not the bottleneck you think it is.
- **Webhooks** for live change detection: issue created/updated/deleted, sprint events.
- **Write path:** create epic + stories, set AC in description or a custom field. Read back status and changelog. No two-way sync in the first version.
- **Config chaos:** custom fields, screens, workflows, and issue types differ per customer. Build a per-org field mapping table on day one; do not hardcode field names.

### Slack
- Bot with `channels:history`, `groups:history`, `chat:write` (for the PM's own sends, not the bot's)
- Intake: mention or emoji reaction on a message routes it to intake
- Thread reading: watch threads where a clarification was sent, capture replies
- Be explicit in your security doc about what the bot reads and retains. A silent listener draws more scrutiny than a chatty one.

### GitHub
- App install, PR/commit metadata only at first: titles, linked issue keys, timestamps, changed paths
- Repo structure and module names for grounding — not source embeddings
- This is the connector to cut if the schedule slips. It's the least load-bearing and the most procurement-sensitive.

---

## 6. Models and cost

- **Frontier** (Claude/GPT class): decompose, critique. These two calls are the product.
- **Small/cheap** (Haiku/mini class, or local): classify, extract, redact, summarize, embed.
- **Local on the 4060:** run the small tier through Ollama during development. An 8GB card runs a 7–8B model at Q4 with roughly 8k usable context; 16GB gets you to ~14B or much longer context. This meaningfully cuts your dev-time API spend. It does **not** run decompose — that job carries request text, retrieved specs, conventions, and glossary, and an 8B at 8k will produce trees PMs throw away. Quality is the product.
- **Interface:** one `LLMProvider` protocol with `generate(messages, schema, model_tier)`. Providers are config. No routing engine.
- **Cost control:** cache retrieval results per request, cap context assembly by token budget, log cost per run and alert per org. At $250/seat you have room, but only if you measure from week one.

---

## 7. Build phases

Week estimates assume one person, full-time, and include the debugging you're not imagining yet. They are optimistic anyway; they always are.

### Phase 0 — Diagnostic (weeks 1–5)
The read-only product. No writes, no Slack, no generation.

1. **W1** — Project skeleton, Postgres schema, tenancy middleware, auth, Docker setup, CI.
2. **W2** — Jira OAuth + resumable history import + changelog ingestion. Test against public Jira instances (Apache's is the largest; there are also published scraped datasets from research papers — verify these yourself, I may have the names wrong).
3. **W3** — Analytics engine: cycle time from transitions, reopen rate, scope-added-after-start, AC-absence rate, estimate vs actual, epic churn.
4. **W4** — Report UI. Evidence links on every number — clicking a churn figure shows the specific issues behind it.
5. **W5** — Harden, package as something you can run on a stranger's Jira in an hour.

**Validation gate at end of W3:** run the analytics against real public Jira history. If the churn signal is noise — if reopen rates don't correlate with anything about spec quality — the thesis needs revisiting before you build Phase 1. This is the single most important checkpoint in the plan, and it costs you three weeks to reach rather than four months.

### Runway gate — week 5
With under six months of runway, this is where you decide:

- **Path A (recommended):** sell the diagnostic as a paid one-off audit. Revenue inside the runway, real Jira access, and a warm list of exactly the buyers for the SaaS. Phase 1 gets funded by audits and built against real data.
- **Path B:** continue straight into Phase 1 on savings, and start outbound in parallel.

Path A is better even ignoring money, because it forces customer contact at week five instead of week seventeen.

### Phase 1 — Core loop (weeks 6–16)
6. **W6–7** — Intake: paste/upload, Slack bot, classification, duplicate detection.
7. **W8–9** — The pipeline: extract, decompose, critique. Schema-constrained outputs. Trace recording.
8. **W10** — Retrieval: embeddings, permission-filtered search, convention profile v1 (hand-seeded from import).
9. **W11** — Diff model and review UI. Accept/edit/reject per node. This UI *is* the product; give it more time than feels reasonable.
10. **W12** — Clarification flow: question drafting, PM sends from own account, thread reading, answers folded back.
11. **W13** — Estimation: comparables retrieval, ranges, evidence display.
12. **W14** — Jira write path + read-back. Field mapping config.
13. **W15** — Needs-attention queue, requirement pipeline board (states, not tasks), trace view.
14. **W16** — Eval harness, accept/reject telemetry, hardening.

### Phase 2 — Change watch (weeks 17–22)
15. Webhook-driven detectors (all four event types), thresholds and digest logic, impact analysis pipeline, proposed diffs on drift, cancelled-epic handling (propose bulk-close, never auto-close).

### Phase 3 — Enterprise readiness (weeks 23+)
16. SSO/SAML, SOC 2 Type I groundwork, admin trace viewer, convention governance (versioning, rollback, approval tiers), retention and deletion jobs, two-way Jira sync if a paying customer demands it.

---

## 8. Evals

Built in Phase 1, not after. Output quality is the product and you cannot feel your way to it.

- **Golden set:** 30–50 real epics from public Jira history with their actual child issues. Compare AI decomposition to what the humans actually built. Score on coverage (did it find the stories that really happened), precision (did it invent work), and AC testability.
- **Telemetry:** accept / edit / reject rate per node type. Every PM edit is a free label. Track edit distance, not just counts.
- **Outcome:** reopen rate on stories from AI-generated specs vs. human-written ones, measured quarterly.
- **Regression gate:** no prompt or model change ships without the golden set run.

Be careful with LLM-as-judge scoring — it correlates with verbosity and agrees with itself. Use it for triage, not for the go/no-go number.

---

## 9. Security checklist (day one, not later)

- Tenancy enforced in one middleware layer, with a test that a cross-tenant query raises
- `project_id` filter applied inside every retrieval query
- Audit log write on every AI proposal and human decision
- Secrets in a manager, never in env files in the repo
- No customer data in prompts sent to providers without a no-training commitment in the contract
- Retention and hard-delete jobs that actually work, tested
- Convention extraction schema-constrained so confidential project content can't leak into an org-wide rule

---

## 10. What not to build

Listing these because each will feel reasonable at 2am in month three:

- A task board with ticket status (you'd have two sources of truth for status)
- Gantt, burndown, timeline, resource workload
- An agent framework or dynamic delegation
- A meeting recording bot
- Two-way sync before a customer demands it
- Per-org fine-tuning
- A document editor beyond what a spec needs
- A separate graph database
- Your own auth

---

## 11. Risk register

| Risk | Mitigation |
|---|---|
| Churn signal isn't real | Week-3 validation gate against public Jira, before Phase 1 |
| Decomposition quality too low | Golden set from week 8; if coverage is poor, the product doesn't work and you need to know in month two |
| Runway runs out pre-revenue | Path A at week 5 — sell the audit |
| Jira config chaos eats weeks | Field mapping table from day one; test against three differently-configured public instances |
| No design partners | The diagnostic is the door-opener; start outreach at week 5, not week 17 |
| Alert noise kills engagement | Digest by default, DM only above an impact threshold, tune with the first real customer |
| Atlassian ships the feature | Speed. Also: they will never ship the rework metric |
| Solo burnout | Phase boundaries are real stopping points; the product is sellable at the end of Phase 0 and again at Phase 1 |

---

## 12. Definition of done, per phase

- **Phase 0:** you can be handed Jira credentials by a stranger and produce an evidence-linked rework report within an hour.
- **Phase 1:** a PM can take a Slack message and end up with an approved, traceable epic in Jira without leaving the loop, and you can show which ambiguities got resolved on the way.
- **Phase 2:** the product tells the PM something they didn't know about their own in-flight epic, at least once a week, without being muted.