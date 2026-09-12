"""ToS gate for the public Jira corpus loader (issue #16)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Repo-relative verification note (source of truth for allow/deny).
TOS_DOC_RELATIVE = Path("_docs") / "public-jira-corpus.md"
SOURCE_ID = "apache_issues"

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


def load_tos_status(*, root: Path | None = None) -> CorpusTosStatus:
    """Parse machine-readable ToS markers from the in-repo verification note."""
    path = tos_doc_path(root=root)
    if not path.is_file():
        raise CorpusTosError(
            f"Public Jira corpus ToS verification note missing: {path}. "
            "See issue #16 — do not run the loader without documented terms."
        )
    text = path.read_text(encoding="utf-8")
    source_m = _SOURCE_RE.search(text)
    date_m = _DATE_RE.search(text)
    status_m = _STATUS_RE.search(text)
    if source_m is None or date_m is None or status_m is None:
        raise CorpusTosError(
            f"Public Jira corpus ToS markers incomplete in {path}. "
            "Expected corpus-tos-source, corpus-tos-verified-date, and "
            "corpus-tos-status (allowed|disallowed)."
        )
    return CorpusTosStatus(
        source=source_m.group("source"),
        verified_date=date_m.group("date"),
        status=status_m.group("status"),
        path=path,
    )


def ensure_tos_allowed(*, root: Path | None = None) -> CorpusTosStatus:
    """Refuse corpus load when verification is missing or status is disallowed."""
    status = load_tos_status(root=root)
    if status.source != SOURCE_ID:
        raise CorpusTosError(
            f"Unexpected corpus-tos-source={status.source!r}; expected {SOURCE_ID!r}"
        )
    if status.status != "allowed":
        raise CorpusTosError(
            f"Public Jira corpus source is marked {status.status!r} in {status.path} "
            f"(verified {status.verified_date}). Loader will not run."
        )
    return status
