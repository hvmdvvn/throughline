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
from throughline.analytics.scope_change import (
    EpicChild,
    EpicScopeResult,
    LateChildDetection,
    SpecChangeDetection,
    SpecField,
    aggregate_scope_signals,
    compute_and_store_scope_change_for_org,
    detect_late_children,
    detect_spec_changes,
    epic_started_at,
    first_in_progress_at,
    measure_epic_scope,
    rebuild_scope_change_aggregates,
    store_late_child_detections,
    store_spec_change_detections,
)
from throughline.analytics.status import StatusLane, classify_status, status_map_classifier

__all__ = [
    "AggregateDimension",
    "CyclePass",
    "CycleTimeResult",
    "EpicChild",
    "EpicScopeResult",
    "IssueDimensions",
    "LateChildDetection",
    "ReopenDetection",
    "SpecChangeDetection",
    "SpecField",
    "StatusLane",
    "aggregate_detections",
    "aggregate_scope_signals",
    "classify_status",
    "compute_and_store_outcomes_for_org",
    "compute_and_store_reopens_for_org",
    "compute_and_store_scope_change_for_org",
    "compute_cycle_time",
    "detect_late_children",
    "detect_reopens",
    "detect_spec_changes",
    "epic_started_at",
    "first_in_progress_at",
    "is_reopen_transition",
    "measure_epic_scope",
    "rebuild_reopen_aggregates",
    "rebuild_scope_change_aggregates",
    "status_map_classifier",
    "store_late_child_detections",
    "store_outcome",
    "store_reopen_detections",
    "store_spec_change_detections",
]
