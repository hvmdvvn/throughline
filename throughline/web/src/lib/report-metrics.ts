/**
 * Stable diagnostic metric keys and UI presentation helpers (issues #22–#25).
 * Keys must match ``throughline.analytics.diagnostic_report``.
 */

export const METRIC_KEYS = {
  CYCLE_TIME_COMPLETED_COUNT: "cycle_time.completed_issue_count",
  CYCLE_TIME_MEDIAN_SECONDS: "cycle_time.median_seconds",
  REOPEN_EVENT_COUNT: "reopen.event_count",
  SCOPE_LATE_CHILD_COUNT: "scope_change.late_child_count",
  SCOPE_SPEC_CHANGE_COUNT: "scope_change.spec_change_count",
  SPEC_QUALITY_ISSUE_COUNT: "spec_quality.issue_count",
  SPEC_QUALITY_AC_MISSING_COUNT: "spec_quality.ac_missing_or_empty_count",
  ESTIMATION_ISSUE_COUNT: "estimation_accuracy.issue_count",
  ESTIMATION_COVERAGE: "estimation_accuracy.coverage",
  ESTIMATION_OVER_COUNT: "estimation_accuracy.over_count",
  ESTIMATION_UNDER_COUNT: "estimation_accuracy.under_count",
  ESTIMATION_ACCURATE_COUNT: "estimation_accuracy.accurate_count",
} as const;

export type MetricKey = (typeof METRIC_KEYS)[keyof typeof METRIC_KEYS];

export type MetricFormat = "count" | "percent" | "rate" | "duration";

export type MetricFigureDef = {
  /** Stable API metric key used for evidence drill-down. */
  metricKey: MetricKey;
  label: string;
  format: MetricFormat;
  hint?: string;
};

export type MetricSectionDef = {
  id: string;
  title: string;
  description: string;
  figures: MetricFigureDef[];
};

/** Primary sales/demo sections required by issue #25. */
export const REPORT_SECTIONS: MetricSectionDef[] = [
  {
    id: "rework",
    title: "Rework",
    description:
      "Done work that returned to an active status (reopen events). Rate uses completed issues in the same report window when available.",
    figures: [
      {
        metricKey: METRIC_KEYS.REOPEN_EVENT_COUNT,
        label: "Reopen events",
        format: "count",
        hint: "Evidence: reopen transitions",
      },
    ],
  },
  {
    id: "scope",
    title: "Scope change",
    description:
      "Late-added epic children and post-start description or acceptance-criteria edits.",
    figures: [
      {
        metricKey: METRIC_KEYS.SCOPE_LATE_CHILD_COUNT,
        label: "Late children",
        format: "count",
      },
      {
        metricKey: METRIC_KEYS.SCOPE_SPEC_CHANGE_COUNT,
        label: "Spec changes after start",
        format: "count",
      },
    ],
  },
  {
    id: "spec-quality",
    title: "Spec quality",
    description:
      "Underspecification indicators — not a composite score. Missing or empty acceptance criteria is called out separately.",
    figures: [
      {
        metricKey: METRIC_KEYS.SPEC_QUALITY_ISSUE_COUNT,
        label: "Issues assessed",
        format: "count",
      },
      {
        metricKey: METRIC_KEYS.SPEC_QUALITY_AC_MISSING_COUNT,
        label: "AC missing or empty",
        format: "count",
      },
    ],
  },
  {
    id: "estimation",
    title: "Estimation accuracy",
    description:
      "Coverage of usable estimates and over / under / accurate buckets for completed work.",
    figures: [
      {
        metricKey: METRIC_KEYS.ESTIMATION_COVERAGE,
        label: "Estimate coverage",
        format: "percent",
      },
      {
        metricKey: METRIC_KEYS.ESTIMATION_OVER_COUNT,
        label: "Over estimate",
        format: "count",
      },
      {
        metricKey: METRIC_KEYS.ESTIMATION_UNDER_COUNT,
        label: "Under estimate",
        format: "count",
      },
      {
        metricKey: METRIC_KEYS.ESTIMATION_ACCURATE_COUNT,
        label: "Accurate",
        format: "count",
      },
      {
        metricKey: METRIC_KEYS.ESTIMATION_ISSUE_COUNT,
        label: "Issues in window",
        format: "count",
      },
    ],
  },
];

/** Keys charted when multiple report versions exist for the same range. */
export const TREND_METRIC_KEYS: MetricKey[] = [
  METRIC_KEYS.REOPEN_EVENT_COUNT,
  METRIC_KEYS.SCOPE_LATE_CHILD_COUNT,
  METRIC_KEYS.SCOPE_SPEC_CHANGE_COUNT,
  METRIC_KEYS.SPEC_QUALITY_AC_MISSING_COUNT,
  METRIC_KEYS.ESTIMATION_COVERAGE,
  METRIC_KEYS.ESTIMATION_OVER_COUNT,
  METRIC_KEYS.ESTIMATION_UNDER_COUNT,
];

export const TREND_LABELS: Record<string, string> = {
  [METRIC_KEYS.REOPEN_EVENT_COUNT]: "Rework (reopens)",
  [METRIC_KEYS.SCOPE_LATE_CHILD_COUNT]: "Late children",
  [METRIC_KEYS.SCOPE_SPEC_CHANGE_COUNT]: "Spec changes",
  [METRIC_KEYS.SPEC_QUALITY_AC_MISSING_COUNT]: "AC missing",
  [METRIC_KEYS.ESTIMATION_COVERAGE]: "Estimate coverage",
  [METRIC_KEYS.ESTIMATION_OVER_COUNT]: "Over estimate",
  [METRIC_KEYS.ESTIMATION_UNDER_COUNT]: "Under estimate",
};

export function metricValue(
  metrics: Record<string, { value: number | null } | undefined>,
  key: string,
): number | null {
  const entry = metrics[key];
  if (!entry || entry.value == null || Number.isNaN(entry.value)) {
    return null;
  }
  return entry.value;
}

/** Rework rate = reopen events / completed issues when both are present. */
export function reworkRate(
  metrics: Record<string, { value: number | null } | undefined>,
): number | null {
  const reopens = metricValue(metrics, METRIC_KEYS.REOPEN_EVENT_COUNT);
  const completed = metricValue(metrics, METRIC_KEYS.CYCLE_TIME_COMPLETED_COUNT);
  if (reopens == null || completed == null || completed <= 0) {
    return null;
  }
  return reopens / completed;
}

export function formatMetricValue(
  value: number | null,
  format: MetricFormat,
): string {
  if (value == null) {
    return "—";
  }
  switch (format) {
    case "percent":
    case "rate":
      return `${(value * 100).toFixed(value * 100 >= 10 ? 0 : 1)}%`;
    case "duration": {
      const seconds = Math.round(value);
      if (seconds < 60) return `${seconds}s`;
      const hours = seconds / 3600;
      if (hours < 48) return `${hours.toFixed(hours >= 10 ? 0 : 1)}h`;
      return `${(hours / 24).toFixed(1)}d`;
    }
    case "count":
    default:
      return Number.isInteger(value)
        ? value.toLocaleString()
        : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
}

/** Pull a human-readable issue / epic key from an evidence ref string. */
export function evidenceDisplayParts(ref: string): {
  primary: string;
  detail: string | null;
} {
  const lateChild = /^epic:([^/]+)\/late-child:([^/]+)$/.exec(ref);
  if (lateChild) {
    return {
      primary: lateChild[2],
      detail: `late child of ${lateChild[1]}`,
    };
  }
  const issue = /^issue:([^/]+)(?:\/(.*))?$/.exec(ref);
  if (issue) {
    return {
      primary: issue[1],
      detail: issue[2] ? issue[2].replace(/:/g, " · ") : null,
    };
  }
  return { primary: ref, detail: null };
}

export function formatDateRange(start: string, end: string): string {
  const opts: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "short",
    day: "numeric",
  };
  const s = new Date(`${start}T00:00:00Z`);
  const e = new Date(`${end}T00:00:00Z`);
  return `${s.toLocaleDateString(undefined, opts)} – ${e.toLocaleDateString(undefined, opts)}`;
}

export function formatTimestamp(iso: string | null): string {
  if (!iso) return "Not generated";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
