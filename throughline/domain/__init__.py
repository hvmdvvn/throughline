"""Core models and business rules (no I/O).

Canonical issue/transition types live here so analytics never depends on
connector clients or Jira field naming (issue #15).
"""

from throughline.domain.issues import CanonicalIssue, CanonicalTransition

__all__ = [
    "CanonicalIssue",
    "CanonicalTransition",
]
