# Signal validation study (issue #26)

Phase 0 go/no-go checkpoint against `_docs/PLAN.md` §7:

> If the churn signal is noise — if reopen rates don't correlate with anything about
> spec quality — the thesis needs revisiting before you build Phase 1.

**Recommendation: revisit** (not go, not hard no-go).

---

## 1. ToS / license verification

Same bar as #16. Sources used:

| Source id | Instance | ToS note | Status |
|---|---|---|---|
| `apache_issues` | https://issues.apache.org/jira | [`_docs/public-jira-corpus.md`](public-jira-corpus.md) § Source A | `allowed` (conditional), verified 2026-09-12 (robots re-checked 2026-09-13) |
| `jenkins_issues` | https://issues.jenkins.io | [`_docs/public-jira-corpus.md`](public-jira-corpus.md) § Source B | `allowed` (conditional), verified 2026-09-13 |

No ToS-failing sources were used. Remote fetches are manual/throttled only; fixtures are small subsets for offline re-runs (not full dumps).

---

## 2. What was run

### 2a. Reproducible fixture study (primary artifact)

Two **distinct** instance-shaped fixture corpora (ASF-shaped `CORPUS-*` + Jenkins-shaped `JENKINS-*`), full import → changelog → normalize → `generate_diagnostic_report`.

```bash
docker compose run --rm api python -m throughline.analytics.signal_validation \
  --fixture --sources apache_issues jenkins_issues \
  --artifact /app/_docs/artifacts/signal-validation-metrics.json
```

| Source | Issues | Jira transitions | Report | Reopen events | Completed cycle-time issues | Median cycle time (s) |
|---|---|---|---|---|---|---|
| `apache_issues` | 2 | 5 | success v1 | **1** | **2** | 100800 |
| `jenkins_issues` | 3 | 9 | success v1 | **2** | **3** | 180000 |

Artifact: [`_docs/artifacts/signal-validation-metrics.json`](artifacts/signal-validation-metrics.json)

**8 / 12** report metrics differ across the two sources (including reopen count, cycle-time median, estimation coverage).

### 2b. Live remote study (ASF + Jenkins REST)

Anonymous, throttled fetches against both live instances (ASF delay ~2s; Jenkins `Crawl-delay: 10`):

```bash
docker compose run --rm api python -m throughline.analytics.signal_validation \
  --remote --sources apache_issues jenkins_issues \
  --page-size 15 --max-pages 2 --max-issues 30
```

One completed remote run (2026-09-13T02:29Z) imported ~30 issues/source from live search + changelog and generated diagnostic reports (`success`). Artifact snapshot: [`_docs/signal-validation-artifacts/study-run-20260913T022936Z.json`](signal-validation-artifacts/study-run-20260913T022936Z.json).

That remote sample used oldest keys (`ORDER BY key ASC` defaults at the time). Observed:

- **Reopen event count = 0** on both live samples
- **Completed cycle-time count = 0** (canonical transitions were not yet force-normalized after import — fixed afterward in the corpus loader)
- **AC field mapping unavailable** on both orgs → `spec_quality.ac_missing_or_empty_count = 0` with all issues `mapping_unavailable`
- Estimation coverage differed (ASF ~0.33 vs Jenkins 0.0)

Follow-up remote re-runs on this shared DB were **blocked by concurrent test wipes** (`ForeignKeyViolation` / `StaleDataError` mid-import). Per issue guidance, when remote is flaky the study may rely on two distinct ToS-recorded fixture subsets — which we did in §2a after fixing normalization.

Default remote JQL is now `ORDER BY updated DESC` for richer changelogs on future re-runs.

---

## 3. Do metrics vary meaningfully?

**Yes, on the fixture corpora (post-normalization fix):** reopen counts (1 vs 2), cycle-time completion and medians, and estimation coverage all move across the two instance-shaped sources. That is enough to say the analytics stack can produce **differentiated** churn/rework and delivery metrics, not a single constant.

**On the completed live remote sample (oldest keys, pre-normalize fix):** almost no cross-source variance in churn metrics (both zero reopens / zero completed cycles). Estimation coverage did vary slightly. That sample is **not** evidence that live public history lacks churn — it is evidence that **sampling + pipeline gaps** can hide it.

---

## 4. Do metrics correlate plausibly with spec quality?

**Not demonstrated.**

PLAN §7 asks whether reopen/churn correlates with **spec quality**. On both fixture and remote public loads:

- No org had an acceptance-criteria field mapping → AC coverage is `mapping_unavailable`, not a real missing/present signal
- Comment-traffic indicators remain blocked on issue #69
- Therefore reopen↔AC-missing co-movement cannot be measured; cross-source notes report insufficient variance / unavailable AC signal

What *does* hold directionally on fixtures: sources with more reopen events also have more issues and longer median cycle times — plausible for rework, but **not** a spec-quality correlation.

---

## 5. What does **not** hold

1. **Public Jira alone does not validate the churn↔spec-quality thesis.** Without AC (and comment) field mappings, “spec quality” proxies are mostly unavailable.
2. **Oldest-key live samples can show zero reopens** even when the detector works on fixture reopen paths — sampling matters.
3. **Canonical transition sync after changelog was incomplete** until the corpus loader began calling `normalize_transitions_for_org` after import; earlier remote reports under-counted cycle time / reopen. Fixed for future runs; do not treat pre-fix remote reopen=0 as proof the signal is absent.
4. **Scope-change (late child / epic) stays near zero** on these corpora — public issues often lack epic linkage in our normalize path (known Phase 0 limitation from #19).
5. **Cross-source correlation with n=2 is not statistically powered** — at best directional.
6. **Concurrent DB wipes break long remote study runs** on a shared local Postgres — remote validation must be run without parallel `pytest` wiping `orgs`.

---

## 6. Explicit recommendation

| Option | Meaning |
|---|---|
| go | Churn signal is real enough to fund Phase 1 |
| no-go | Churn signal is noise; stop Phase 1 on this thesis |
| **revisit** | **Chosen** — machinery works and metrics can vary, but PLAN’s required correlation with spec quality is **unproven** on public data |

**Revisit before Phase 1** until at least one of:

1. A public (or partner) Jira with mapped AC / description conventions yields a measurable reopen↔underspecification relationship, or
2. A paid diagnostic on a real customer Jira (Path A in PLAN §7) supplies mapped fields and enough rework volume to re-test the thesis.

Do **not** treat fixture reopen variance as customer validation. Do **not** block all Phase 0 packaging/hardening work — only the Phase 1 build decision gated on this thesis.

---

## 7. How to re-run (QA)

```bash
# ToS markers must remain allowed for both sources:
#   _docs/public-jira-corpus.md

docker compose up -d db redis

# Offline / CI-safe (≥2 distinct instance fixtures):
docker compose run --rm api python -m throughline.analytics.signal_validation \
  --fixture --sources apache_issues jenkins_issues

# Optional live (no concurrent pytest; honors rate limits):
docker compose run --rm api python -m throughline.analytics.signal_validation \
  --remote --sources apache_issues jenkins_issues \
  --page-size 15 --max-pages 2 --max-issues 30

# Unit/integration:
docker compose run --rm api pytest tests/test_signal_validation.py tests/test_jira_corpus.py -q
```

Expect fixture artifact reopen counts ≥1 on at least one source after the normalize-after-import fix. Live remote results will depend on JQL sample and field mappings.

---

## 8. Code / docs touched

- `_docs/public-jira-corpus.md` — Jenkins source ToS block + ASF robots re-check
- `throughline/ingest/corpus/{tos,loader,__main__,__init__}.py` — multi-source ToS + Jenkins remote/fixture
- `throughline/ingest/corpus/data/jenkins/` — Jenkins fixture subset
- `throughline/analytics/signal_validation/` — study runner CLI
- `_docs/artifacts/signal-validation-metrics.json` — fixture comparison artifact
- `_docs/signal-validation-artifacts/` — live remote run snapshot
- `AGENTS.md` — commands + documents table
- `tests/test_signal_validation.py`, `tests/test_jira_corpus.py`
