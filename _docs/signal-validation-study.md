# Signal validation study (issue #26)

Phase 0 go/no-go checkpoint against `_docs/PLAN.md` §7: *if the churn signal is
noise — if reopen rates don't correlate with anything about spec quality — the
thesis needs revisiting before Phase 1*.

**Study date:** 2026-09-13  
**Recommendation:** **revisit** (do not treat the churn↔spec-quality thesis as
validated on public corpora; Phase 1 should not proceed on that claim alone)

---

## ToS verification

Both live sources were verified before use. Machine-readable markers and
narrative are in [`_docs/public-jira-corpus.md`](public-jira-corpus.md):

| Source id | Instance | Verified | Status |
|---|---|---|---|
| `apache_issues` | https://issues.apache.org/jira | 2026-09-12 (robots re-check 2026-09-13) | `allowed` (conditional) |
| `jenkins_issues` | https://issues.jenkins.io | 2026-09-13 | `allowed` (conditional) |

Constraints (no full dumps in git; throttle; honor 429 / crawl-delay; local DB
only) were followed. Fixtures under `throughline/ingest/corpus/data/` are small
offline subsets for CI / re-runs, not redistributable bulk datasets.

---

## What was run

### A. Offline fixture study (reproducible)

```bash
docker compose run --rm api python -m throughline.analytics.signal_validation --fixture
```

- Sources: `apache_issues` + `jenkins_issues` fixture packs
- Artifact: [`_docs/artifacts/signal-validation-metrics.json`](artifacts/signal-validation-metrics.json)

### B. Live remote study (primary evidence)

```bash
docker compose run --rm api python -m throughline.analytics.signal_validation \
  --remote --max-pages 1 --page-size 15 --max-issues 15 \
  --artifact /app/_docs/artifacts/signal-validation-metrics-remote.json
```

- ASF JQL default: `project = KAFKA ORDER BY key ASC` (first page)
- Jenkins JQL default: `project = JENKINS ORDER BY key ASC` (first page)
- Delays: ASF ≥2s; Jenkins crawl-delay 10s
- Artifact: [`_docs/artifacts/signal-validation-metrics-remote.json`](artifacts/signal-validation-metrics-remote.json)
- Report status: `success` on both orgs (diagnostic report v1 each)

Runner: `python -m throughline.analytics.signal_validation`  
Loader: `python -m throughline.ingest.corpus --remote --source <id>`

---

## Results (remote / primary)

| Metric | ASF (`apache_issues`) | Jenkins (`jenkins_issues`) | Varies? |
|---|---:|---:|---|
| Issues imported (loader count) | 15 | 30* | yes |
| Status transitions (loader) | 12 | 22 | yes |
| `reopen.event_count` | 0 | 0 | no |
| `cycle_time.completed_issue_count` | 0 | 2 | yes |
| `cycle_time.median_seconds` | null | ~277558039 | — |
| `scope_change.spec_change_count` | 8 | 0 | yes |
| `scope_change.late_child_count` | 0 | 0 | no |
| `spec_quality.issue_count` | 15 | 15 | no |
| `spec_quality.ac_missing_or_empty_count` | 0 | 0 | no† |
| `estimation_accuracy.coverage` | 0.0 | 0.0 | no |

\* Jenkins loader count on this remote pass was higher than `--max-issues 15`
because the shared local DB retained rows from earlier aborted attempts before
org-scoped reset + org-filtered counts were fixed. Analytics snapshots above
still show 15 issues in estimation/spec-quality issue counts for both sources.
Re-runs after this fix report clean per-source counts (see fixture artifact).
† Zero AC-missing with `mapping_unavailable` semantics — public projects have
no Throughline AC field mapping, so “missing AC” is not a usable comparator.

### Fixture study (secondary)

On synthetic packs (clean re-run: ASF 2 issues / 5 transitions, Jenkins 3 / 9),
reopen counts **do** differ (`apache_issues`: 1, `jenkins_issues`: 2) and 8/12
metric keys show cross-source deltas. That proves the pipeline can surface
variation; it does **not** validate the product thesis on real public history.

---

## What holds

1. **Pipeline end-to-end works** on two distinct public Jira instances
   (import → changelog → normalize → analytics #17–#21 → versioned diagnostic
   report #22).
2. **Some metrics vary across sources** on the live sample (completed cycle
   counts; description/spec field-change counts).
3. **ToS-gated multi-source loading is enforceable** in code and docs.

---

## What does **not** hold

1. **Reopen / churn signal is not visible** in the live early-key samples
   (`reopen.event_count = 0` on both ASF and Jenkins). Ordering by `key ASC`
   biases toward oldest issues; even so, this run does not show a usable
   reopen rate differential.
2. **Reopen ↔ spec-quality correlation cannot be tested** here: AC field
   mappings are absent on both public orgs, so AC-missing indicators stay at
   zero / mapping-unavailable and cannot co-vary with reopen.
3. **Estimation accuracy is inert** (coverage 0.0 both sides) — public issues
   rarely carry usable original estimates / story points in the imported
   fields.
4. **Fixture reopen variance is not evidence** that real customer Jira churn
   correlates with underspecification; fixtures are shaped for smoke coverage.
5. **Sample size is small** (one page / ~15 issues per source) — insufficient
   for statistical claims even if reopen had fired.

---

## Explicit recommendation

**Revisit** the Phase 0 churn-signal thesis before building Phase 1.

- The PLAN gate asks whether reopen rates correlate with something about spec
  quality. This study **does not** establish that correlation on public data.
- Honest reading: public OSS trackers are a weak proxy for the PM/spec workflow
  Throughline targets (AC fields, estimates, epic hygiene). A **go** would
  require either (a) a customer-shaped corpus with AC mappings and reopen
  density, or (b) a narrowed thesis (e.g. “scope-change and cycle-time alone
  are sellable diagnostics”) documented as a product decision.
- Engineering readiness for the diagnostic path is fine; **product go** on the
  churn↔spec thesis is not earned by this checkpoint.

This is a successful issue outcome: negative evidence was recorded rather than
tuned away.

---

## Re-run checklist

1. Confirm ToS markers still `allowed` in `_docs/public-jira-corpus.md`.
2. `docker compose up -d db redis` (or full stack).
3. Fixture: `docker compose run --rm api python -m throughline.analytics.signal_validation --fixture`
4. Remote: command in §B above (expect several minutes; Jenkins crawl-delay 10).
5. Compare new JSON under `_docs/artifacts/` to this write-up.
