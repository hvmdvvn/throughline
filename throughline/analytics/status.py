"""Status lane classification for cycle-time analytics (issue #17).

Connector-agnostic: analytics never call Jira. Callers may supply an explicit
name→lane map (preferred when discovery data is available) or rely on the
default name heuristics used in tests and local corpora.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum


class StatusLane(StrEnum):
    """Coarse workflow lane used for cycle-time boundaries."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    UNKNOWN = "unknown"


# Lowercased status names → lane. Kept narrow; prefer an explicit map in prod.
_DEFAULT_DONE = frozenset(
    {
        "done",
        "closed",
        "resolved",
        "complete",
        "completed",
        "cancelled",
        "canceled",
    }
)
_DEFAULT_IN_PROGRESS = frozenset(
    {
        "in progress",
        "in development",
        "in review",
        "code review",
        "review",
        "testing",
        "qa",
        "in qa",
    }
)
_DEFAULT_TODO = frozenset(
    {
        "to do",
        "todo",
        "open",
        "backlog",
        "selected for development",
        "new",
    }
)


def classify_status(status: str | None) -> StatusLane:
    """Map a status display name to a workflow lane via default heuristics."""
    if status is None:
        return StatusLane.UNKNOWN
    key = status.strip().lower()
    if not key:
        return StatusLane.UNKNOWN
    if key in _DEFAULT_DONE:
        return StatusLane.DONE
    if key in _DEFAULT_IN_PROGRESS:
        return StatusLane.IN_PROGRESS
    if key in _DEFAULT_TODO:
        return StatusLane.TODO
    return StatusLane.UNKNOWN


def status_map_classifier(
    mapping: Mapping[str, StatusLane],
) -> Callable[[str | None], StatusLane]:
    """Build a classifier from an explicit status-name → lane map (case-insensitive)."""
    normalized = {name.strip().lower(): lane for name, lane in mapping.items()}

    def _classify(status: str | None) -> StatusLane:
        if status is None:
            return StatusLane.UNKNOWN
        key = status.strip().lower()
        if not key:
            return StatusLane.UNKNOWN
        return normalized.get(key, StatusLane.UNKNOWN)

    return _classify
