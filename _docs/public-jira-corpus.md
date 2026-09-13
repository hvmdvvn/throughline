# Public Jira test corpus — terms verification

Machine-readable status blocks (parsed by the corpus loader). Each source has its
own markers; the loader refuses a source unless its block is present and
`corpus-tos-status: allowed`.

```text
corpus-tos-source: apache_issues
corpus-tos-verified-date: 2026-09-12
corpus-tos-status: allowed
```

```text
corpus-tos-source: jenkins_issues
corpus-tos-verified-date: 2026-09-13
corpus-tos-status: allowed
```

---

## Source A — ASF Jira (`apache_issues`)

| Field | Value |
|---|---|
| Name | ASF Jira (Apache Software Foundation) |
| Site | https://issues.apache.org/jira |
| REST base (Server API v2) | https://issues.apache.org/jira/rest/api/2 |
| Deployment | Jira Server / Data Center (not Jira Cloud) |
| Access model | Anonymous read where project permissions allow “Browse projects” for anonymous users |

### Documents reviewed (2026-09-12; robots re-checked 2026-09-13)

1. **ASF Jira landing / account policy** — https://issues.apache.org/jira — public signup disabled; many projects remain anonymously readable.
2. **ASF rate limiting** — https://infra.apache.org/blog/rate-limiting-on-apache-services — Jira is rate-limited; automated clients must honor HTTP 429 and slow down.
3. **ASF abuse / connectivity policy** — https://infra.apache.org/abc/ — free services; abusive load may be blocked; bots should respect `robots.txt` and 429 responses.
4. **`robots.txt` on issues.apache.org** — path-specific `Disallow` for search/export views (`/sr/`, `/si/`, charts, some `/secure/` pages). REST search/issue paths are not listed as disallowed. Automated clients must still throttle and honor HTTP 429. (Earlier notes cited a blanket `Disallow: /`; the live file as of 2026-09-13 does not.)
5. **ASF privacy policy** — https://www.apache.org/foundation/policies/privacy.html — covers site logging; does not grant a redistribution license for issue tracker content.

No separate ASF “open data dump license” was found for Jira issue payloads. Issue content is publicly browsable for permitted projects but is **not** treated here as a redistributable bulk dataset.

### Conclusion (`apache_issues`)

**Status: `allowed` (conditional)** for Throughline local/dev corpus use, under these constraints:

| Allowed | Not allowed |
|---|---|
| Fixture / sample subset checked into the repo for CI and offline smoke loads | Hosting a full remote dump in git |
| Optional **manual** remote REST fetch of anonymously readable issues into a **local** DB | Hitting the live site from CI or unbounded scrapers |
| Throttled requests that honor HTTP 429 and keep a polite delay between calls | Ignoring rate limits or `robots.txt` bot rules for HTML crawl |
| Reusing the same import + changelog + normalization path as customer imports | Treating this verification as a license to republish ASF issue content |

---

## Source B — Jenkins Jira (`jenkins_issues`)

| Field | Value |
|---|---|
| Name | Jenkins project Jira |
| Site | https://issues.jenkins.io |
| REST base (Server API v2) | https://issues.jenkins.io/rest/api/2 |
| Deployment | Jira Server / Data Center (public browse confirmed 2026-09-13) |
| Access model | Anonymous browse of public projects (e.g. `JENKINS`); core tracking has largely moved to GitHub, but historical / remaining Jira issues remain anonymously readable |

### Documents reviewed (2026-09-13)

1. **Jenkins Jira project UI** — https://issues.jenkins.io/projects/JENKINS/summary — public project summary without login.
2. **Anonymous browse confirmation (Jenkins infra)** — https://github.com/jenkins-infra/helpdesk/issues/5076 — maintainers confirm unauthenticated browse remains intentional; instance is not meant to be private.
3. **`robots.txt` on issues.jenkins.io** — default `User-agent: *` disallows issue navigator export views (`/sr/`, `/si/`), charts, and some `/secure/` pages; **`Crawl-delay: 10`**. REST `/rest/api/2/*` is not disallowed; clients must honor the crawl-delay and HTTP 429.
4. **Linux Foundation website Terms of Use** — https://www.linuxfoundation.org/legal/terms — governs LF websites; does not grant a blanket redistribution license for third-party Jira issue payloads. User-submitted content remains subject to applicable rights; Throughline treats issue JSON as browsable-for-local-analysis only, not a redistributable dump.
5. **Jenkins project governance / conduct** — https://www.jenkins.io/project/governance/ and https://www.jenkins.io/project/conduct/ — community norms; no separate “open Jira dump” license found.

### Conclusion (`jenkins_issues`)

**Status: `allowed` (conditional)** for Throughline local/dev corpus and Phase 0 signal-validation use, under these constraints:

| Allowed | Not allowed |
|---|---|
| Small fixture / sample subsets for offline study re-runs | Hosting a full remote dump in git |
| Optional **manual** remote REST fetch into a **local** DB | CI or unbounded scrapers against the live site |
| Throttled requests honoring **Crawl-delay: 10**, HTTP 429, and path disallows | Ignoring `robots.txt` / rate limits |
| Same import + changelog + normalization path as customer imports | Republishing Jenkins issue content or treating ToS as a dataset license |

---

## Loader commands

See `AGENTS.md` / README:

- `python -m throughline.ingest.corpus` / `--fixture` — CI-safe ASF-shaped fixture subset
- `python -m throughline.ingest.corpus --remote` — optional live ASF fetch (`apache_issues`)
- `python -m throughline.ingest.corpus --remote --source jenkins_issues` — optional live Jenkins fetch
- Signal validation study (issue #26): `python -m throughline.analytics.signal_validation`

If any source’s `corpus-tos-status` is changed to `disallowed`, or its markers are removed, the loader must refuse that source.
