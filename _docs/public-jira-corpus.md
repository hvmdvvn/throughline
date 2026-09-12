# Public Jira test corpus — terms verification

Machine-readable status (parsed by the corpus loader):

```text
corpus-tos-source: apache_issues
corpus-tos-verified-date: 2026-09-12
corpus-tos-status: allowed
```

## Source

| Field | Value |
|---|---|
| Name | ASF Jira (Apache Software Foundation) |
| Site | https://issues.apache.org/jira |
| REST base (Server API v2) | https://issues.apache.org/jira/rest/api/2 |
| Deployment | Jira Server 8.x (not Jira Cloud) |
| Access model | Anonymous read where project permissions allow “Browse projects” for anonymous users |

## Documents reviewed (2026-09-12)

1. **ASF Jira landing / account policy** — https://issues.apache.org/jira — public signup disabled; many projects remain anonymously readable.
2. **ASF rate limiting** — https://infra.apache.org/blog/rate-limiting-on-apache-services — Jira is rate-limited; automated clients must honor HTTP 429 and slow down.
3. **ASF abuse / connectivity policy** — https://infra.apache.org/abc/ — free services; abusive load may be blocked; bots should respect `robots.txt` and 429 responses.
4. **`robots.txt` on issues.apache.org** — default `User-agent: *` is `Disallow: /` with crawl-delay guidance; unrestricted HTML crawling / bot scraping is not permitted without ASF allow-listing.
5. **ASF privacy policy** — https://www.apache.org/foundation/policies/privacy.html — covers site logging; does not grant a redistribution license for issue tracker content.

No separate ASF “open data dump license” was found for Jira issue payloads. Issue content is publicly browsable for permitted projects but is **not** treated here as a redistributable bulk dataset.

## Conclusion

**Status: `allowed` (conditional)** for Throughline local/dev corpus use, under these constraints:

| Allowed | Not allowed |
|---|---|
| Fixture / sample subset checked into the repo for CI and offline smoke loads | Hosting a full remote dump in git |
| Optional **manual** remote REST fetch of anonymously readable issues into a **local** DB | Hitting the live site from CI or unbounded scrapers |
| Throttled requests that honor HTTP 429 and keep a polite delay between calls | Ignoring rate limits or `robots.txt` bot rules for HTML crawl |
| Reusing the same import + changelog + normalization path as customer imports | Treating this verification as a license to republish ASF issue content |

If `corpus-tos-status` is changed to `disallowed`, or this file / its markers are removed, the loader must refuse to run.

## Loader commands

See `AGENTS.md` / README: `python -m throughline.ingest.corpus --fixture` (default / CI) and optional `--remote` for manual live load.
