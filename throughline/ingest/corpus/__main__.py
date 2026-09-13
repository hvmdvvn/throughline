"""CLI: ``python -m throughline.ingest.corpus`` (issues #16 / #26)."""

from __future__ import annotations

import argparse
import sys

from throughline.db.session import get_session_factory
from throughline.ingest.corpus.loader import (
    DEFAULT_REMOTE_JQL,
    DEFAULT_REMOTE_MAX_ISSUES,
    DEFAULT_REMOTE_MAX_PAGES,
    DEFAULT_REMOTE_MIN_DELAY_SECONDS,
    DEFAULT_REMOTE_PAGE_SIZE,
    SOURCE_CONFIGS,
    load_corpus,
)
from throughline.ingest.corpus.tos import SOURCE_ID, CorpusTosError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Load the public Jira test corpus into the local DB. "
            "Requires ToS verification in _docs/public-jira-corpus.md. "
            "Default: fixture subset (CI). Use --remote for optional live fetch."
        )
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Load the in-repo sample subset (default when --remote is omitted)",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Optional manual fetch from a ToS-verified public Jira (not for CI)",
    )
    parser.add_argument(
        "--source",
        default=SOURCE_ID,
        choices=sorted(SOURCE_CONFIGS),
        help=f"Public source id for --remote (default: {SOURCE_ID})",
    )
    parser.add_argument(
        "--jql",
        default=None,
        help=f"Remote JQL (default depends on --source; ASF default: {DEFAULT_REMOTE_JQL!r})",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_REMOTE_PAGE_SIZE,
        help="Remote search page size",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_REMOTE_MAX_PAGES,
        help="Remote search page cap (safety limit)",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_REMOTE_MAX_ISSUES,
        help="Remote changelog issue cap (safety limit)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=None,
        help=(
            "Minimum seconds between remote HTTP calls "
            f"(default: per-source; ASF {DEFAULT_REMOTE_MIN_DELAY_SECONDS})"
        ),
    )
    args = parser.parse_args(argv)

    load_mode = "remote" if args.remote else "fixture"
    Session = get_session_factory()
    try:
        with Session() as db:
            result = load_corpus(
                db,
                mode=load_mode,
                source=args.source,
                jql=args.jql,
                page_size=args.page_size,
                max_pages=args.max_pages,
                max_issues=args.max_issues,
                min_delay_seconds=args.min_delay,
            )
    except CorpusTosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"ok source={result.source} mode={result.mode} org_id={result.org_id} "
        f"issues={result.issue_count} transitions={result.transition_count} "
        f"tos_verified={result.tos_verified_date}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
