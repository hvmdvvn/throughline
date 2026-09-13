"""CLI: ``python -m throughline.analytics.signal_validation`` (issue #26)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from throughline.analytics.signal_validation import (
    DEFAULT_RANGE_END,
    DEFAULT_RANGE_START,
    DEFAULT_STUDY_SOURCES,
    parse_iso_date,
    run_signal_validation_study,
    study_to_dict,
    write_study_artifact,
)
from throughline.db.session import get_session_factory
from throughline.ingest.corpus.loader import SOURCE_CONFIGS
from throughline.ingest.corpus.tos import CorpusTosError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 0 signal validation study against ≥2 ToS-verified "
            "public Jira sources and write a comparison artifact."
        )
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Use in-repo fixture subsets (default when --remote is omitted)",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Optional manual live fetch (rate-limited; not for CI)",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=list(DEFAULT_STUDY_SOURCES),
        choices=sorted(SOURCE_CONFIGS),
        help="Source ids to compare (default: apache_issues jenkins_issues)",
    )
    parser.add_argument(
        "--range-start",
        default=DEFAULT_RANGE_START.isoformat(),
        help="Report range start (ISO date)",
    )
    parser.add_argument(
        "--range-end",
        default=DEFAULT_RANGE_END.isoformat(),
        help="Report range end (ISO date)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
        help="Remote search page size",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Remote search page cap",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=40,
        help="Remote changelog issue cap per source",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=None,
        help="Override per-source remote delay (seconds)",
    )
    parser.add_argument(
        "--artifact",
        default=None,
        help="Optional path for metrics JSON (default: _docs/artifacts/...)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print summary only; do not write the artifact file",
    )
    args = parser.parse_args(argv)

    mode = "remote" if args.remote else "fixture"
    if len(args.sources) < 2:
        print("error: need at least two --sources", file=sys.stderr)
        return 2

    Session = get_session_factory()
    try:
        with Session() as db:
            study = run_signal_validation_study(
                db,
                sources=tuple(args.sources),
                mode=mode,
                range_start=parse_iso_date(args.range_start),
                range_end=parse_iso_date(args.range_end),
                page_size=args.page_size,
                max_pages=args.max_pages,
                max_issues=args.max_issues,
                min_delay_seconds=args.min_delay,
            )
            artifact_path = None
            if not args.no_write:
                artifact_path = write_study_artifact(
                    study,
                    path=Path(args.artifact) if args.artifact else None,
                )
    except CorpusTosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"ok mode={mode} sources={','.join(args.sources)}")
    for note in study.notes:
        print(f"note: {note}")
    for src in study.sources:
        print(
            f"source={src.source} org={src.org_id} issues={src.issue_count} "
            f"transitions={src.transition_count} report={src.report_status} "
            f"v{src.report_version}"
        )
    if artifact_path is not None:
        print(f"artifact={artifact_path}")
    payload = study_to_dict(study)
    print(f"metric_delta_keys={len(payload['metric_deltas'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
