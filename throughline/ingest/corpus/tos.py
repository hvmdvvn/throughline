"""ToS gate for the public Jira corpus loader (issues #16 / #26)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Repo-relative verification note (source of truth for allow/deny).
TOS_DOC_RELATIVE = Path("_docs") / "public-jira-corpus.md"
SOURCE_ID = "apache_issues"
JENKINS_SOURCE_ID = "jenkins_issues"
KNOWN_SOURCES = frozenset({SOURCE_ID, JENKINS_SOURCE_ID})

_STATUS_RE = re.compile(
    r"^corpus-tos-status:\s*(?P<status>allowed|disallowed)\s*$",
    re.MULTILINE,
)
_SOURCE_RE = re.compile(
    r"^corpus-tos-source:\s*(?P<source>\S+)\s*$",
    re.MULTILINE,
)
_DATE_RE = re.compile(
    r"^corpus-tos-verified-date:\s*(?P<date>\S+)\s*$",
    re.MULTILINE,
)
# One fenced ```text ... ``` block that contains all three markers.
_BLOCK_RE = re.compile(
    r"```text\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


class CorpusTosError(RuntimeError):
    """Raised when corpus ToS notes are missing or mark the source as disallowed."""


@dataclass(frozen=True)
class CorpusTosStatus:
    """Parsed verification markers from ``_docs/public-jira-corpus.md``."""

    source: str
    verified_date: str
    status: str
    path: Path


def repo_root() -> Path:
    """Return the repository root (parent of ``throughline/``)."""
    return Path(__file__).resolve().parents[3]


def tos_doc_path(*, root: Path | None = None) -> Path:
    return (root or repo_root()) / TOS_DOC_RELATIVE


def _parse_block(body: str, *, path: Path) -> CorpusTosStatus | None:
    source_m = _SOURCE_RE.search(body)
    date_m = _DATE_RE.search(body)
    status_m = _STATUS_RE.search(body)
    if source_m is None or date_m is None or status_m is None:
        return None
    return CorpusTosStatus(
        source=source_m.group("source"),
        verified_date=date_m.group("date"),
        status=status_m.group("status"),
        path=path,
    )


def load_tos_statuses(*, root: Path | None = None) -> dict[str, CorpusTosStatus]:
    """Parse all machine-readable ToS blocks from the verification note."""
    path = tos_doc_path(root=root)
    if not path.is_file():
        raise CorpusTosError(
            f"Public Jira corpus ToS verification note missing: {path}. "
            "See issue #16 — do not run the loader without documented terms."
        )
    text = path.read_text(encoding="utf-8")
    statuses: dict[str, CorpusTosStatus] = {}
    for match in _BLOCK_RE.finditer(text):
        parsed = _parse_block(match.group("body"), path=path)
        if parsed is None:
            continue
        statuses[parsed.source] = parsed
    if not statuses:
        raise CorpusTosError(
            f"Public Jira corpus ToS markers incomplete in {path}. "
            "Expected one or more ```text blocks with corpus-tos-source, "
            "corpus-tos-verified-date, and corpus-tos-status (allowed|disallowed)."
        )
    return statuses


def load_tos_status(
    *,
    root: Path | None = None,
    source: str = SOURCE_ID,
) -> CorpusTosStatus:
    """Parse ToS markers for a single source (default: ``apache_issues``)."""
    statuses = load_tos_statuses(root=root)
    status = statuses.get(source)
    if status is None:
        raise CorpusTosError(
            f"Public Jira corpus ToS markers missing for source={source!r} in "
            f"{tos_doc_path(root=root)}. Known sources in note: "
            f"{sorted(statuses)}."
        )
    return status


def ensure_tos_allowed(
    *,
    root: Path | None = None,
    source: str = SOURCE_ID,
) -> CorpusTosStatus:
    """Refuse corpus load when verification is missing or status is disallowed."""
    if source not in KNOWN_SOURCES:
        raise CorpusTosError(
            f"Unknown corpus source={source!r}; expected one of {sorted(KNOWN_SOURCES)}"
        )
    status = load_tos_status(root=root, source=source)
    if status.status != "allowed":
        raise CorpusTosError(
            f"Public Jira corpus source {source!r} is marked {status.status!r} in "
            f"{status.path} (verified {status.verified_date}). Loader will not run."
        )
    return status
