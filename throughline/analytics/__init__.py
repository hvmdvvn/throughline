"""Phase 0 analytics: cycle time, churn metrics (plan §2 ``analytics/``).

Computations consume canonical issues/transitions only — no connector clients.
"""

from throughline.analytics.cycle_time import (
    CyclePass,
    CycleTimeResult,
    compute_and_store_outcomes_for_org,
    compute_cycle_time,
    store_outcome,
)
from throughline.analytics.reopen import (
    AggregateDimension,
    IssueDimensions,
    ReopenDetection,
    aggregate_detections,
    compute_and_store_reopens_for_org,
    detect_reopens,
    is_reopen_transition,
    rebuild_reopen_aggregates,
    store_reopen_detections,
)
from throughline.analytics.status import StatusLane, classify_status, status_map_classifier

__all__ = [
    "AggregateDimension",
    "CyclePass",
    "CycleTimeResult",
    "IssueDimensions",
    "ReopenDetection",
    "StatusLane",
    "aggregate_detections",
    "classify_status",
    "compute_and_store_outcomes_for_org",
    "compute_and_store_reopens_for_org",
    "compute_cycle_time",
    "detect_reopens",
    "is_reopen_transition",
    "rebuild_reopen_aggregates",
    "status_map_classifier",
    "store_outcome",
    "store_reopen_detections",
]
