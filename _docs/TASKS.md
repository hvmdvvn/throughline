# TASKS.md — Throughline backlog

Stack: Django + Django REST Framework, Celery + Redis, Postgres 16 with pgvector, Next.js frontend, Docker throughout. Django admin is the ops and support UI.

Each task is self-contained. Phase boundaries are marked but tasks within a phase can largely be picked up in any order once the foundation (1–9) exists.

---

# Foundation

## 1. Empty project with a passing test
Goal: A Django project that runs and has one green test.
Description: Create the repository, a Django project with a single `core` app, and pytest with pytest-django configured. Write one trivial test asserting the app imports and the settings load. Commit with a README stating how to run the tests.

## 2. Docker Compose development environment
Goal: `docker compose up` gives a working web, worker, database, and cache.
Description: Write a Dockerfile for the Django app and a compose file with services for web, Postgres 16, and Redis. Ensure the app container can reach the database and that the test suite runs inside the container. No application features are needed — just the environment.

## 3. Postgres with pgvector enabled
Goal: The vector extension is available and provably working.
Description: Extend the Postgres image or init script to install and enable pgvector. Add a Django migration that creates the extension, plus a throwaway model with a vector field. Write a test that stores and retrieves a vector to prove the round trip works.

## 4. Settings and secrets handling
Goal: Configuration is environment-driven with no secrets in the repo.
Description: Split Django settings into base/dev/prod, load configuration from environment variables using django-environ or similar, and document every required variable in `.env.example`. Add a check that refuses to boot in production without the required secrets set.

## 5. CI pipeline
Goal: Every push runs lint, migrations check, and tests.
Description: Set up GitHub Actions to install dependencies, spin up Postgres, run `makemigrations --check`, run ruff, and run pytest. The pipeline should fail on missing migrations, which is the most common solo-developer mistake in Django.

## 6. Tenancy models
Goal: Organization, User, and Membership models exist and are visible in the admin.
Description: Create models for `Org`, a custom `User`, and `Membership` linking them with a role field (admin, pm, viewer). Register all three in the Django admin with sensible list displays. Every future model will carry an `org` foreign key, so establish the abstract base model with `org`, `created_at`, `updated_at`, and a soft-delete flag here.

## 7. Tenancy enforcement layer
Goal: Cross-tenant data access is impossible by construction.
Description: Implement a request-scoped current-organization mechanism (middleware plus a thread-local or contextvar) and a base manager that filters every queryset by it. Write tests that prove a query from Org A cannot return Org B's rows, including one test that deliberately tries to bypass it and is expected to fail loudly.

## 8. Authentication
Goal: Users can sign in, and the API authenticates requests.
Description: Integrate a hosted auth provider (Clerk or WorkOS) using JWT verification in DRF, rather than building auth. Map the provider's user identity onto the local `User` and `Membership` records on first login. SSO comes later but choosing a provider that supports it now avoids a migration.

## 9. Celery and background jobs
Goal: Long-running work can be queued, retried, and observed.
Description: Wire Celery to Redis, add a worker service to compose, and implement one example task that sleeps and logs. Configure retries with exponential backoff and result storage, and add a way to see task status from the Django admin.

---

# Phase 0 — Diagnostic

## 10. Jira OAuth connection
Goal: An org can connect its Jira Cloud site and the tokens are stored.
Description: Register an Atlassian OAuth 2.0 (3LO) app and implement the authorization flow, requesting offline access so refresh tokens are available. Store credentials encrypted per org, with refresh handled transparently. A connection status page in the admin is enough UI for now.

## 11. Jira API client wrapper
Goal: A reusable client that handles pagination, rate limits, and retries.
Description: Build a thin client around the Jira REST v3 API that transparently follows pagination, respects `Retry-After` headers, and backs off on 429s. All Jira calls in the codebase should go through it. Include tests against recorded fixtures rather than the live API.

## 12. Jira project and field discovery
Goal: The system knows what projects, issue types, and custom fields an instance has.
Description: Fetch and store the connected site's projects, issue types, statuses, and custom field definitions. Create a per-org field mapping table that records which custom field holds acceptance criteria, story points, and similar. Every Jira instance is configured differently, so nothing may be hardcoded.

## 13. Issue history import job
Goal: A full backfill of an instance's issues, resumable after failure.
Description: Write a Celery task that pages through issues using JQL and stores them, recording a cursor so an interrupted run continues rather than restarting. Real instances hold 100k+ issues, so assume the job will fail partway and design for that. Track import progress on the connection record.

## 14. Changelog import
Goal: Every status transition for imported issues is stored.
Description: For each imported issue, fetch its changelog and store status transitions with timestamps and actors. This is the raw material for cycle time and reopen detection, so accuracy matters more than speed. Extend the resumable cursor from the import job to cover this pass.

## 15. Canonical issue normalization
Goal: Jira's shape is translated into a stable internal model.
Description: Define internal models for issues and transitions that are independent of Jira's field naming, and write the mapping layer that populates them using the per-org field mapping. This is the boundary that makes Linear or Azure DevOps possible later, so keep Jira specifics out of it.

## 16. Public Jira test corpus
Goal: A realistic dataset to develop and validate against.
Description: Build a loader that imports issue history from a public Jira instance (Apache's is the largest well-known one) into a local development database. Verify the source and its terms of use yourself before relying on it. This corpus is what the analytics work is developed and validated against.

## 17. Cycle time computation
Goal: Time-in-status and end-to-end cycle time per issue.
Description: From stored transitions, compute time spent in each status and total time from first in-progress to done, handling reopens and multiple passes. Store results in an outcomes table rather than computing on read. Include tests with hand-constructed transition sequences covering weird cases like backwards moves.

## 18. Reopen and rework metrics
Goal: Quantify how often finished work came back.
Description: Detect issues that moved from a done status back to an active one, count occurrences, and aggregate by epic, project, and time period. Store each detection with a link to the evidence so the UI can drill down. Resist the urge to declare causes — this measures reopens, not blame.

## 19. Scope change metrics
Goal: Measure work added to an epic after it started.
Description: For each epic, identify child issues created after the epic's first child entered in-progress, and aggregate the volume and timing. Also capture issues whose description or acceptance criteria changed after work began. These two signals are the closest proxy for spec instability.

## 20. Spec quality metrics
Goal: Measure how underspecified the historical work was.
Description: Compute per-issue indicators such as missing or empty acceptance criteria, description length, and how much clarification traffic appears in comments. Aggregate by project and team. These are crude proxies and should be presented as indicators, not scores.

## 21. Estimation accuracy metrics
Goal: Compare what was estimated to what actually happened.
Description: For issues with story points or original estimates, compare against measured cycle time and produce per-team distributions of over- and under-estimation. Handle the common case where estimates are missing or inconsistently applied by reporting coverage alongside accuracy. Never produce per-person figures.

## 22. Report aggregation model
Goal: One stored report object containing all diagnostic findings.
Description: Define a report model that holds the computed metrics for a given org and date range, along with references to the evidence rows behind each number. Add a Celery task that generates a report end to end. Reports should be reproducible and versioned so results can be compared over time.

## 23. Report API
Goal: The frontend can fetch a report and drill into any number.
Description: Expose DRF endpoints for listing reports, fetching a report's metrics, and retrieving the underlying evidence rows for a given metric. Paginate evidence lists, since some will be large. Permissions follow the tenancy layer.

## 24. Frontend shell
Goal: A running Next.js app with authentication and navigation.
Description: Set up the Next.js project with Tailwind and shadcn/ui, integrate the auth provider, and build the app shell with navigation placeholders for the four eventual destinations. No feature content is needed. Configure it to call the Django API with authenticated requests.

## 25. Diagnostic report UI
Goal: A readable report where every number is clickable.
Description: Build the report screen showing rework rate, scope change, spec quality indicators, and estimation accuracy, with trends over time. Clicking any figure opens the list of specific issues behind it. This is the artifact you show a stranger in a sales conversation, so presentation quality matters more than usual.

## 26. Signal validation study
Goal: Evidence that the churn signal is real, not noise.
Description: Run the full analytics pipeline against at least two different public Jira instances and examine whether the metrics vary meaningfully across projects and correlate with each other in plausible ways. Write up the findings honestly, including what does not hold. This is the go/no-go checkpoint before building Phase 1.

## 27. Diagnostic onboarding flow
Goal: A stranger can connect Jira and get a report unattended.
Description: Chain the OAuth connection, import, changelog fetch, analytics run, and report generation into one guided flow with progress feedback. Handle the slow path gracefully, since a full import can take a long time. Send an email when the report is ready.

---

# Phase 1 — Core loop

## 28. LLM provider interface
Goal: One abstraction for all model calls, provider-agnostic.
Description: Define a provider protocol with a method taking messages, an output schema, and a model tier, returning parsed structured output. Implement it for one frontier provider, using Pydantic models for schema-constrained responses. All pipeline code calls this interface, never a vendor SDK directly.

## 29. Local model provider
Goal: Cheap pipeline steps can run against a local model.
Description: Add an Ollama-backed implementation of the provider interface for the small-model tier, configurable per environment. This is for development-time cost reduction on classification and extraction work, not for generation. Include a fallback to the hosted small model when the local one is unavailable.

## 30. Run tracing
Goal: Every model call is recorded with inputs, outputs, and cost.
Description: Create a `runs` table capturing pipeline step, model, prompt inputs, retrieved context identifiers, raw output, token counts, cost, and latency. Wrap the provider interface so tracing is automatic rather than remembered. Expose runs in the Django admin for debugging.

## 31. Cost monitoring
Goal: Per-org spend is visible and bounded.
Description: Aggregate cost from the runs table by org and time period, expose it in the admin, and add a configurable threshold that alerts when an org exceeds it. Add a hard cap that refuses further generation for the period. Margin depends on this existing before customers do.

## 32. Request intake model
Goal: Raw incoming requests can be stored from any source.
Description: Create a `Request` model holding raw text, source type, author identity, external references, and processing state. Build the paste and file-upload entry points and a simple list view. Everything downstream consumes this model, so keep it source-agnostic.

## 33. Transcript intake
Goal: Meeting transcripts can be submitted as requests.
Description: Accept pasted or uploaded transcripts, split them into speaker-attributed segments, and store them as a request with segment structure preserved. Requirements are often decided in meetings, so segment attribution matters for later evidence links. No integration with notetaker tools yet.

## 34. Slack app and OAuth
Goal: An org can install the Slack app and the bot is authenticated.
Description: Create the Slack app with the scopes needed to read channel and thread history and store per-org installation tokens. Handle the event subscription URL verification and signature validation. Document precisely which scopes are requested and why, for the eventual security review.

## 35. Slack intake trigger
Goal: A Slack message can be turned into a request.
Description: Implement an emoji reaction and a bot mention as intake triggers, capturing the message text, author, channel, and permalink into a `Request`. Acknowledge in-thread so the user knows it registered. Handle the case where the same message is triggered twice.

## 36. Embeddings and vector storage
Goal: Text can be embedded and searched, scoped by project.
Description: Add an embeddings table with a pgvector column and a mandatory non-null project reference, plus a service for embedding and upserting text. Implement similarity search that always takes a project filter as a required argument, not an optional one. Test that an unscoped search is impossible to express.

## 37. Permission-filtered retrieval
Goal: Retrieval never returns content the requesting user cannot see.
Description: Layer project-permission filtering over vector search, deriving a user's visible projects from synced Jira project permissions. Write tests proving a restricted project's content cannot surface for an unauthorized user. This is a breach if it is wrong, so test it adversarially.

## 38. Request classification
Goal: Incoming requests are sorted before any expensive work happens.
Description: Build the classification pipeline step that labels a request as bug, duplicate, too-small, or needs-spec, using the small model tier with a constrained output schema. Route each label to a different downstream path, including a lightweight single-story path for small items. Store the classification with its confidence.

## 39. Duplicate detection
Goal: Requests matching existing work are flagged, not re-specced.
Description: On intake, run a vector search against existing requirements and recent requests, and flag likely duplicates with links to the matches. Use retrieval rather than asking a model to judge from memory. Present duplicates as a suggestion for the PM to confirm, never as an automatic rejection.

## 40. Requirement node models
Goal: The epic/story/AC tree exists as data with full version history.
Description: Create `RequirementNode` with a self-referential parent, a type field, state, and body; `NodeLink` for typed edges such as derived-from and conflicts-with; and `NodeVersion` recording every change. Write a recursive CTE query for fetching an entire tree efficiently. Register everything in the admin.

## 41. Extraction step
Goal: Structured facts are pulled out of raw request text.
Description: Build the pipeline step that extracts actors, entities, constraints, and explicit asks from a request using the small model tier with a strict output schema. Keep each extracted item linked to the source text span, since those spans become the evidence shown in the UI later. Store results against the request.

## 42. Decomposition step
Goal: A request becomes a proposed epic/story/AC tree.
Description: Build the frontier-model step that takes the request, extracted facts, retrieved similar past specs, and the convention profile, and produces a structured tree. Use a schema-constrained output so the result is parsed, not scraped. Record which retrieved items influenced the output for evidence display.

## 43. Critique step
Goal: Gaps in a proposed tree are surfaced as flags.
Description: Build a separate frontier-model call with an adversarial prompt that identifies ambiguity, untestable acceptance criteria, missing edge cases, and conflicts with existing approved specs. Output flags with a severity and a reference to the specific node. Keep it separate from decomposition — a model critiquing its own output in one pass is weaker.

## 44. Convention profile
Goal: The org's spec-writing conventions are stored and injected.
Description: Create a conventions model with rule text, scope, a formatting-versus-semantic tier, status, and version history, plus an editable admin view. Seed it from imported Jira history by extracting recurring structural patterns with schema-constrained output. Inject active rules into decomposition prompts.

## 45. Domain glossary
Goal: Org-specific terms are captured and used in generation.
Description: Extract recurring entities, systems, and terms from imported history into a glossary table, and include relevant entries in generation context. Keep extraction schema-constrained so confidential project content cannot leak into an org-wide artifact. Make the glossary editable by admins.

## 46. Estimation step
Goal: Each story gets a range with visible comparables.
Description: For each proposed story, retrieve similar historical stories and their measured cycle times, and produce a range from that distribution rather than a model guess. Store the comparable node identifiers alongside the range so the UI can show them. Never output a single point estimate.

## 47. Diff assembly
Goal: Pipeline output becomes a reviewable proposal, never a direct write.
Description: Create a `Diff` model holding proposed changes, author type, rationale, evidence references, and status, and have the pipeline emit diffs instead of mutating requirement nodes. Implement accept and reject transitions that apply or discard changes atomically with a version record. This is the core safety primitive of the product.

## 48. Diff review API
Goal: Diffs can be listed, inspected, and decided through the API.
Description: Expose endpoints for pending diffs, a diff's detail with before/after per node, and accept/reject/edit actions. Record who decided what and when for the audit trail. Support partial acceptance — accepting some nodes while rejecting others.

## 49. Diff review UI
Goal: A PM can review a proposed tree node by node.
Description: Build the review screen showing the proposed tree with per-node accept, edit, and reject controls, inline critique flags, and estimate ranges. Show the evidence behind each node on demand. This is the screen the product lives or dies on, so give it more iteration than it seems to need.

## 50. Clarification drafting
Goal: Open ambiguities become sendable questions.
Description: Convert critique flags into concise, plain-language questions aimed at the original requester, grouped so one message can carry several. Let the PM edit or drop any question before sending. Store each question with a link back to the flag and node it came from.

## 51. Clarification sending and thread watching
Goal: Questions go out from the PM and answers come back automatically.
Description: Implement sending via the PM's own Slack identity rather than the bot, and register the resulting thread for watching. When replies arrive in a watched thread, capture them against the corresponding clarification record. The bot reads but does not speak.

## 52. Answer incorporation
Goal: Answers update the requirement tree as a reviewable diff.
Description: Take captured answers, re-run the relevant pipeline steps with the new information, and emit a diff proposing updates to the affected nodes. Mark the originating flags as resolved when accepted. Nothing changes in the tree without the PM accepting.

## 53. Jira write path
Goal: An approved tree is created in Jira.
Description: Create the epic and child issues in Jira from an approved tree, placing acceptance criteria according to the org's field mapping and storing the resulting issue keys against the nodes. Make the operation idempotent so a partial failure can be retried without duplicates. Report failures clearly rather than silently.

## 54. Jira read-back
Goal: What happens to pushed issues flows back into the system.
Description: Poll or subscribe for status and field changes on issues linked to requirement nodes, and record them against the node's outcome history. This is what closes the loop for the rework metric. No writes back to Jira from this path.

## 55. Needs-attention queue
Goal: The home screen shows what requires the PM today.
Description: Build the queue aggregating pending diffs, unanswered clarifications, unresolved flags, and detected changes, sorted by impact. Make each item one click from its resolution screen. Empty state matters — it should feel like completion, not like a broken page.

## 56. Requirement pipeline board
Goal: A board showing requirements by state, not tasks by status.
Description: Build a column view over requirement states: intake, drafting, questions out, awaiting answers, approved, pushed, watching. Cards are requirements and move as their state changes. This deliberately does not show Jira ticket status — that would create a second source of truth.

## 57. Trace and evidence view
Goal: Any generated item can be traced to what produced it.
Description: Build the view showing, for a given node, the originating request lines, the retrieved past specs, the conventions applied, the comparables used for estimation, and the downstream Jira issue. Source it from the runs table and node links. This is the trust feature and the one competitors cannot easily copy.

## 58. Audit log
Goal: Every AI proposal and human decision is permanently recorded.
Description: Implement an append-only audit log capturing actor, action, target, and timestamp for all diffs, decisions, sends, and pushes, with no update or delete path. Expose it to org admins with filtering. Security reviews will ask for this specifically.

## 59. Golden evaluation set
Goal: A benchmark of real epics with their actual outcomes.
Description: Assemble 30 to 50 epics from the public Jira corpus along with the child issues that were really created, and store them in a stable format for repeated evaluation. Record what makes each case interesting. This set is the only objective quality signal available before customers exist.

## 60. Evaluation harness
Goal: Decomposition quality can be scored on every change.
Description: Build a runner that executes the pipeline over the golden set and scores coverage, precision, and acceptance-criteria testability, storing results per run for comparison. Wire it so prompt and model changes cannot ship without a run. Treat model-as-judge scores as triage only, not as the deciding number.

## 61. Quality telemetry
Goal: Real user edits become training signal.
Description: Record accept, edit, and reject events per node type, including edit distance between proposed and final text, and aggregate them into a quality dashboard. Every PM edit is a free label about what the model got wrong. Keep the data available for later prompt and retrieval improvements.

---

# Phase 2 — Change watch

## 62. Jira webhook receiver
Goal: Live Jira events arrive, are verified, and are stored.
Description: Register webhooks for issue created, updated, and deleted plus sprint events, validate their authenticity, and store raw payloads in an append-only table before any processing. Process asynchronously so a slow pipeline never blocks delivery. Handle replays and out-of-order arrival.

## 63. Change detectors
Goal: Four kinds of meaningful change are detected from the event stream.
Description: Implement detectors for amended requests touching approved epics, issues drifting from their acceptance criteria, new issues appearing under an approved epic, and delivery signals such as slips and reopens. Each detector emits a typed change event with evidence references. Keep them independent so one can be tuned or disabled alone.

## 64. Notification thresholds and digest
Goal: Signal reaches the PM without becoming noise.
Description: Add per-detector impact scoring and a configurable threshold, routing high-impact changes to an immediate Slack DM and everything else to a daily digest. Default to conservative thresholds. A noisy first week means a muted channel and a dead product.

## 65. Impact analysis
Goal: A detected change explains what it affects.
Description: For a change event, traverse the requirement graph to identify affected nodes, dependent stories, and linked Jira issues, and generate a concise summary of what would need to change. Emit the result as a proposed diff where applicable. Show the traversal as evidence rather than asserting conclusions.

## 66. Cancellation handling
Goal: Cancelling an epic proposes cleanup rather than performing it.
Description: When an approved, pushed epic is cancelled, mark the tree cancelled and generate a proposed bulk-close diff for its Jira issues, requiring explicit PM confirmation. Never auto-close issues — in-flight work may be attached to them. Record the cancellation as a rework data point.

## 67. GitHub integration
Goal: PR and commit activity informs actuals and grounding.
Description: Build the GitHub App installation flow and ingest pull request and commit metadata, linking it to Jira issue keys found in branch names, titles, and messages. Store repository structure and module names for generation context. Do not index source code.

## 68. Retention and deletion
Goal: Data can be retained and deleted according to policy.
Description: Implement configurable retention windows per data category and a verified hard-delete path for an org's data, including embeddings and raw event payloads. Write a test that proves deletion is complete across every table. Customers will ask about this in the first security conversation.