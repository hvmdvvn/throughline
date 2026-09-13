"""Public Jira test corpus loader (issues #16 / #26).

Drives the same ``run_issue_history_import`` + ``run_changelog_import`` path as
customer imports. Default mode loads the in-repo fixture subset (CI-safe).
Optional ``--remote`` fetches from a ToS-verified public Jira Server instance.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from throughline.connectors.jira.client import JiraClient
from throughline.connectors.jira.crypto import encrypt_secret
from throughline.connectors.jira.import_changelog import run_changelog_import
from throughline.connectors.jira.import_history import run_issue_history_import
from throughline.db.models import (
    JiraConnection,
    JiraConnectionStatus,
    JiraIssue,
    JiraStatusTransition,
    Membership,
    MembershipRole,
    Org,
    User,
)
from throughline.ingest.corpus.tos import (
    SOURCE_ID,
    CorpusTosError,
    ensure_tos_allowed,
)
from throughline.tenancy import skip_tenant_enforcement, use_org

logger = logging.getLogger(__name__)

CORPUS_ORG_NAME = "Public Jira Corpus"
CORPUS_AUTH_SUBJECT = "corpus-local-dev"
APACHE_SITE_URL = "https://issues.apache.org/jira"
APACHE_REST_BASE = "https://issues.apache.org/jira/rest/api/2"
JENKINS_SITE_URL = "https://issues.jenkins.io"
JENKINS_REST_BASE = "https://issues.jenkins.io/rest/api/2"
# Placeholder cloud id for the connection row (Server has no Atlassian cloud id).
CORPUS_CLOUD_ID = "apache-issues-public"
# Prefer recently updated issues (richer changelogs) over oldest keys.
DEFAULT_REMOTE_JQL = "project = KAFKA ORDER BY updated DESC"
DEFAULT_REMOTE_PAGE_SIZE = 25
DEFAULT_REMOTE_MAX_PAGES = 2
DEFAULT_REMOTE_MAX_ISSUES = 50
# ASF: polite delay; Jenkins robots.txt Crawl-delay: 10.
DEFAULT_REMOTE_MIN_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class CorpusSourceConfig:
    """Remote endpoint + org identity for one ToS-verified public Jira."""

    source_id: str
    org_name: str
    auth_subject: str
    site_url: str
    rest_base: str
    cloud_id: str
    site_name: str
    default_jql: str
    min_delay_seconds: float


SOURCE_CONFIGS: dict[str, CorpusSourceConfig] = {
    SOURCE_ID: CorpusSourceConfig(
        source_id=SOURCE_ID,
        org_name=CORPUS_ORG_NAME,
        auth_subject=CORPUS_AUTH_SUBJECT,
        site_url=APACHE_SITE_URL,
        rest_base=APACHE_REST_BASE,
        cloud_id=CORPUS_CLOUD_ID,
        site_name="ASF Jira (public corpus)",
        default_jql=DEFAULT_REMOTE_JQL,
        min_delay_seconds=DEFAULT_REMOTE_MIN_DELAY_SECONDS,
    ),
    "jenkins_issues": CorpusSourceConfig(
        source_id="jenkins_issues",
        org_name="Public Jira Corpus — Jenkins",
        auth_subject="corpus-jenkins-local-dev",
        site_url=JENKINS_SITE_URL,
        rest_base=JENKINS_REST_BASE,
        cloud_id="jenkins-issues-public",
        site_name="Jenkins Jira (public corpus)",
        default_jql="project = JENKINS ORDER BY updated DESC",
        min_delay_seconds=10.0,
    ),
}
_CHANGELOG_PATH = re.compile(r"/issue/(?P<key>[^/]+)/changelog/?$")
_DATA_DIR = Path(__file__).resolve().parent / "data"
# Per-source fixture roots (ASF sample stays at data/; Jenkins under data/jenkins/).
FIXTURE_DIRS: dict[str, Path] = {
    SOURCE_ID: _DATA_DIR,
    "jenkins_issues": _DATA_DIR / "jenkins",
}


@dataclass(frozen=True)
class CorpusLoadResult:
    """Summary after fixture or remote corpus load."""

    org_id: uuid.UUID
    mode: str
    issue_count: int
    transition_count: int
    tos_verified_date: str
    source: str = SOURCE_ID


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def fixture_data_dir(*, source: str = SOURCE_ID) -> Path:
    path = FIXTURE_DIRS.get(source)
    if path is None:
        raise CorpusTosError(
            f"No fixture directory for source={source!r}; "
            f"expected one of {sorted(FIXTURE_DIRS)}"
        )
    return path


def _load_json(data_dir: Path, name: str) -> Any:
    path = data_dir / name
    if not path.is_file():
        raise FileNotFoundError(f"Corpus fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_opener(*, source: str = SOURCE_ID):
    """Serve the sample search page + per-issue Cloud-shaped changelogs."""
    data_dir = fixture_data_dir(source=source)
    search = _load_json(data_dir, "search_page.json")
    issues = search.get("issues") if isinstance(search, dict) else None
    if not isinstance(issues, list):
        raise FileNotFoundError(f"Corpus fixture search_page.json missing issues: {data_dir}")
    changelogs: dict[str, Any] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = issue.get("key")
        if not isinstance(key, str) or not key:
            continue
        changelogs[key] = _load_json(data_dir, f"changelog_{key}.json")

    def opener(req: Any, timeout: float = 60) -> _FakeResponse:
        _ = timeout
        parts = urlsplit(req.full_url)
        path = parts.path
        if path.endswith("/search"):
            return _FakeResponse(search)
        match = _CHANGELOG_PATH.search(path)
        if match:
            key = match.group("key")
            if key not in changelogs:
                raise AssertionError(f"No changelog fixture for {key}")
            return _FakeResponse(changelogs[key])
        raise AssertionError(f"Unexpected corpus fixture path: {path}")

    return opener


def _server_changelog_to_cloud(
    payload: dict[str, Any],
    *,
    start_at: int,
    max_results: int,
) -> dict[str, Any]:
    """Reshape Jira Server ``expand=changelog`` histories into Cloud ``/changelog`` pages."""
    changelog = payload.get("changelog")
    histories: list[Any] = []
    if isinstance(changelog, dict) and isinstance(changelog.get("histories"), list):
        histories = changelog["histories"]
    total = len(histories)
    page = histories[start_at : start_at + max_results]
    return {
        "startAt": start_at,
        "maxResults": max_results,
        "total": total,
        "isLast": start_at + len(page) >= total,
        "values": page,
    }


def _remote_server_opener(
    *,
    rest_base: str,
    min_delay_seconds: float,
    sleeper: Any | None = None,
):
    """Anonymous opener for public Jira Server; adapts changelog to Cloud-shaped pages."""
    sleep = sleeper or time.sleep
    last_call_at = {"t": 0.0}

    def _throttle() -> None:
        now = time.monotonic()
        wait = min_delay_seconds - (now - last_call_at["t"])
        if wait > 0:
            sleep(wait)
        last_call_at["t"] = time.monotonic()

    def opener(req: Any, timeout: float = 60) -> Any:
        parts = urlsplit(req.full_url)
        match = _CHANGELOG_PATH.search(parts.path)
        if match:
            key = match.group("key")
            query = parse_qs(parts.query)
            start_at = int(query.get("startAt", ["0"])[0])
            max_results = int(query.get("maxResults", ["100"])[0])
            issue_url = (
                f"{rest_base}/issue/{key}"
                f"?expand=changelog&fields=summary,status"
            )
            _throttle()
            issue_req = Request(
                issue_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "throughline-corpus",
                },
                method="GET",
            )
            with urlopen(issue_req, timeout=timeout) as resp:
                raw = resp.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected issue payload for {key}")
            shaped = _server_changelog_to_cloud(
                payload, start_at=start_at, max_results=max_results
            )
            return _FakeResponse(shaped)

        _throttle()
        # Forward search (and any other) calls anonymously.
        headers = {"Accept": "application/json", "User-Agent": "throughline-corpus"}
        fwd = Request(req.full_url, data=req.data, headers=headers, method=req.get_method())
        return urlopen(fwd, timeout=timeout)

    return opener


def ensure_corpus_org(
    db: Session,
    *,
    source: str = SOURCE_ID,
) -> Org:
    """Create or reuse the dedicated local corpus org + connected Jira row."""
    cfg = SOURCE_CONFIGS.get(source)
    if cfg is None:
        raise CorpusTosError(
            f"Unknown corpus source={source!r}; expected one of {sorted(SOURCE_CONFIGS)}"
        )

    org = db.scalar(
        skip_tenant_enforcement(select(Org).where(Org.name == cfg.org_name))
    )
    if org is None:
        org = Org(name=cfg.org_name)
        db.add(org)
        db.flush()

    user = db.scalar(
        skip_tenant_enforcement(
            select(User).where(User.auth_subject == cfg.auth_subject)
        )
    )
    if user is None:
        user = User(
            auth_subject=cfg.auth_subject,
            email=f"{cfg.auth_subject}@localhost",
            display_name=f"Corpus Loader ({cfg.source_id})",
        )
        db.add(user)
        db.flush()

    membership = db.scalar(
        skip_tenant_enforcement(
            select(Membership).where(
                Membership.org_id == org.id,
                Membership.user_id == user.id,
            )
        )
    )
    if membership is None:
        db.add(
            Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
        )

    with use_org(org.id):
        connection = db.scalar(select(JiraConnection))
        if connection is None:
            connection = db.scalar(
                skip_tenant_enforcement(
                    select(JiraConnection).where(JiraConnection.org_id == org.id)
                )
            )
        if connection is None:
            connection = JiraConnection(org_id=org.id)
            db.add(connection)
            db.flush()
        connection.deleted_at = None
        connection.cloud_id = cfg.cloud_id
        connection.site_url = cfg.site_url
        connection.site_name = cfg.site_name
        connection.encrypted_access_token = encrypt_secret("corpus-anonymous")
        connection.encrypted_refresh_token = encrypt_secret("corpus-anonymous")
        connection.access_token_expires_at = datetime.now(UTC) + timedelta(days=365)
        connection.status = JiraConnectionStatus.CONNECTED
        connection.status_detail = f"public corpus ({cfg.source_id}; anonymous / fixture)"
        db.commit()
        db.refresh(org)
    return org


def _count_issues(db: Session, org_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(JiraIssue)
            .where(JiraIssue.org_id == org_id, JiraIssue.deleted_at.is_(None))
        )
        or 0
    )


def _count_transitions(db: Session, org_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(JiraStatusTransition)
            .where(
                JiraStatusTransition.org_id == org_id,
                JiraStatusTransition.deleted_at.is_(None),
            )
        )
        or 0
    )


def load_corpus(
    db: Session,
    *,
    mode: str = "fixture",
    source: str = SOURCE_ID,
    jql: str | None = None,
    page_size: int = DEFAULT_REMOTE_PAGE_SIZE,
    max_pages: int | None = None,
    max_issues: int | None = None,
    min_delay_seconds: float | None = None,
    root: Path | None = None,
) -> CorpusLoadResult:
    """Import corpus issues + status transitions into the local DB.

    Always checks ``_docs/public-jira-corpus.md`` first. ``mode=fixture`` is the
    CI path (ASF-shaped synthetic subset). ``mode=remote`` is optional/manual
    against a ToS-verified public instance (``source``).
    """
    if mode not in {"fixture", "remote"}:
        raise ValueError("mode must be 'fixture' or 'remote'")
    if source not in SOURCE_CONFIGS:
        raise CorpusTosError(
            f"Unknown corpus source={source!r}; "
            f"expected one of {sorted(SOURCE_CONFIGS)}"
        )
    cfg = SOURCE_CONFIGS[source]
    tos = ensure_tos_allowed(root=root, source=source)

    org = ensure_corpus_org(db, source=source)
    delay = (
        cfg.min_delay_seconds if min_delay_seconds is None else min_delay_seconds
    )

    with use_org(org.id):
        if mode == "fixture":
            client = JiraClient.from_base_url(
                base_url="https://corpus.local/rest/api/2",
                access_token="fixture",
                opener=_fixture_opener(source=source),
            )
            history = run_issue_history_import(
                db,
                client,
                org.id,
                jql=jql or "ORDER BY key ASC",
                page_size=50,
            )
            changelog = run_changelog_import(db, client, org.id, page_size=100)
        else:
            remote_max_pages = DEFAULT_REMOTE_MAX_PAGES if max_pages is None else max_pages
            remote_max_issues = DEFAULT_REMOTE_MAX_ISSUES if max_issues is None else max_issues
            client = JiraClient.from_base_url(
                base_url=cfg.rest_base,
                anonymous=True,
                opener=_remote_server_opener(
                    rest_base=cfg.rest_base,
                    min_delay_seconds=delay,
                ),
            )
            history = run_issue_history_import(
                db,
                client,
                org.id,
                jql=jql or cfg.default_jql,
                page_size=page_size,
                max_pages=remote_max_pages,
            )
            changelog = run_changelog_import(
                db,
                client,
                org.id,
                page_size=100,
                max_issues=remote_max_issues,
            )

        # Ensure analytics tables see canonical transitions even if per-issue
        # sync during changelog import was skipped/partial (issue #26).
        from throughline.ingest.normalize import (
            normalize_issues_for_org,
            normalize_transitions_for_org,
        )

        normalize_issues_for_org(db, org.id)
        normalize_transitions_for_org(db, org.id)
        db.commit()

        issue_count = _count_issues(db, org.id)
        transition_count = _count_transitions(db, org.id)
        logger.info(
            "corpus load complete source=%s mode=%s issues=%s transitions=%s "
            "history_done=%s changelog_done=%s",
            source,
            mode,
            issue_count,
            transition_count,
            history.completed,
            changelog.completed,
        )
        if issue_count < 1 or transition_count < 1:
            raise CorpusTosError(
                f"Corpus smoke check failed: issues={issue_count} transitions={transition_count}"
            )

    return CorpusLoadResult(
        org_id=org.id,
        mode=mode,
        issue_count=issue_count,
        transition_count=transition_count,
        tos_verified_date=tos.verified_date,
        source=source,
    )
