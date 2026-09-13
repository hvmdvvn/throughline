"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useMemo, useState } from "react";

import { EvidencePanel, type EvidenceTarget } from "@/components/diagnostic/evidence-panel";
import { TrendSparkline } from "@/components/diagnostic/trend-sparkline";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  getReport,
  listReports,
  type DiagnosticReportDetail,
  type DiagnosticReportListItem,
} from "@/lib/api";
import {
  METRIC_KEYS,
  REPORT_SECTIONS,
  TREND_LABELS,
  TREND_METRIC_KEYS,
  formatDateRange,
  formatMetricValue,
  formatTimestamp,
  metricValue,
  reworkRate,
  type MetricFormat,
  type MetricKey,
} from "@/lib/report-metrics";
import { cn } from "@/lib/utils";

type TrendSeries = {
  metricKey: MetricKey;
  label: string;
  format: MetricFormat;
  points: { label: string; value: number }[];
};

function statusTone(status: string): string {
  switch (status) {
    case "success":
      return "text-foreground";
    case "partial":
      return "text-amber-700 dark:text-amber-400";
    case "failed":
      return "text-destructive";
    case "pending":
      return "text-muted-foreground";
    default:
      return "text-muted-foreground";
  }
}

function pickDefaultReport(
  reports: DiagnosticReportListItem[],
): DiagnosticReportListItem | null {
  if (reports.length === 0) return null;
  const usable = reports.find(
    (r) => r.status === "success" || r.status === "partial",
  );
  return usable ?? reports[0];
}

export function DiagnosticReportView() {
  const { getToken, isLoaded, isSignedIn } = useAuth();
  const [reports, setReports] = useState<DiagnosticReportListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DiagnosticReportDetail | null>(null);
  const [rangeDetails, setRangeDetails] = useState<DiagnosticReportDetail[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null);

  const openEvidence = useCallback(
    (metricKey: string, label: string, format: MetricFormat) => {
      if (!detail) return;
      setEvidence({
        reportId: detail.id,
        metricKey,
        label,
        format,
      });
    },
    [detail],
  );

  const loadList = useCallback(async () => {
    setListLoading(true);
    setError(null);
    try {
      const token = await getToken();
      if (!token) {
        throw new Error("No Clerk session token available.");
      }
      const items = await listReports(token);
      setReports(items);
      const initial = pickDefaultReport(items);
      setSelectedId(initial?.id ?? null);
      if (!initial) {
        setDetail(null);
        setRangeDetails([]);
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "Failed to load reports";
      setError(message);
      setReports([]);
      setSelectedId(null);
      setDetail(null);
    } finally {
      setListLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    if (!isLoaded) return;
    if (!isSignedIn) {
      setListLoading(false);
      setError("Sign in required to load diagnostic reports.");
      return;
    }
    void loadList();
  }, [isLoaded, isSignedIn, loadList]);

  useEffect(() => {
    if (!selectedId || !isSignedIn) return;

    let cancelled = false;

    async function loadDetail(reportId: string) {
      setDetailLoading(true);
      setError(null);
      try {
        const token = await getToken();
        if (!token) {
          throw new Error("No Clerk session token available.");
        }
        const report = await getReport(token, reportId);
        if (cancelled) return;
        setDetail(report);

        const siblings = reports
          .filter(
            (r) =>
              r.range_start === report.range_start &&
              r.range_end === report.range_end &&
              (r.status === "success" || r.status === "partial"),
          )
          .slice(0, 8);

        if (siblings.length <= 1) {
          setRangeDetails([report]);
        } else {
          const fetched = await Promise.all(
            siblings.map(async (s) => {
              if (s.id === report.id) return report;
              return getReport(token, s.id);
            }),
          );
          if (cancelled) return;
          const ordered = [...fetched].sort((a, b) => {
            const av = a.generated_at ?? a.created_at;
            const bv = b.generated_at ?? b.created_at;
            return av.localeCompare(bv);
          });
          setRangeDetails(ordered);
        }
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "Failed to load report";
        setError(message);
        setDetail(null);
        setRangeDetails([]);
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    }

    void loadDetail(selectedId);
    return () => {
      cancelled = true;
    };
  }, [selectedId, reports, getToken, isSignedIn]);

  const trends: TrendSeries[] = useMemo(() => {
    if (rangeDetails.length < 2) return [];
    const series: TrendSeries[] = [];
    for (const key of TREND_METRIC_KEYS) {
      const points = rangeDetails
        .map((r) => {
          const value = metricValue(r.metrics, key);
          if (value == null) return null;
          const stamp = r.generated_at ?? r.created_at;
          return {
            label: `v${r.version}`,
            value,
            sort: stamp,
          };
        })
        .filter((p): p is { label: string; value: number; sort: string } => p != null);
      if (points.length < 2) continue;
      const format: MetricFormat =
        key === METRIC_KEYS.ESTIMATION_COVERAGE ? "percent" : "count";
      series.push({
        metricKey: key,
        label: TREND_LABELS[key] ?? key,
        format,
        points: points.map(({ label, value }) => ({ label, value })),
      });
    }
    return series;
  }, [rangeDetails]);

  const rate = detail ? reworkRate(detail.metrics) : null;

  if (!isLoaded || listLoading) {
    return (
      <div className="mx-auto max-w-4xl space-y-6" aria-busy="true">
        <div className="space-y-2">
          <div className="h-8 w-56 animate-pulse rounded bg-muted" />
          <div className="h-4 w-80 max-w-full animate-pulse rounded bg-muted" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-28 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
        <p className="text-sm text-muted-foreground">Loading diagnostic reports…</p>
      </div>
    );
  }

  if (error && reports.length === 0 && !detail) {
    return (
      <div className="mx-auto max-w-2xl space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          Diagnostic report
        </h1>
        <p className="text-sm text-destructive">{error}</p>
        <p className="text-sm text-muted-foreground">
          Confirm the API is running and Clerk session JWT matches the API
          issuer. Reports are tenancy-scoped via membership.
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => void loadList()}>
          Retry
        </Button>
      </div>
    );
  }

  if (reports.length === 0) {
    return (
      <div className="mx-auto max-w-2xl space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          Diagnostic report
        </h1>
        <p className="text-sm text-muted-foreground">
          No diagnostic reports for this organization yet. Generate one via the
          diagnostic report job after Jira history is imported — this screen
          stays empty until a versioned report exists.
        </p>
      </div>
    );
  }

  return (
    <div className="relative mx-auto max-w-4xl">
      {evidence ? (
        <button
          type="button"
          className="fixed inset-0 z-30 bg-foreground/20"
          aria-label="Dismiss evidence panel"
          onClick={() => setEvidence(null)}
        />
      ) : null}
      <EvidencePanel target={evidence} onClose={() => setEvidence(null)} />

      <header className="mb-8 space-y-4 border-b border-border pb-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              Diagnostic report
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Evidence-linked Phase 0 metrics for this org. Click any figure to
              open the issues behind it.
            </p>
          </div>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Report
            <select
              className="h-8 min-w-[14rem] rounded-lg border border-border bg-background px-2 text-sm text-foreground"
              value={selectedId ?? ""}
              onChange={(e) => {
                setEvidence(null);
                setSelectedId(e.target.value);
              }}
            >
              {reports.map((r) => (
                <option key={r.id} value={r.id}>
                  {formatDateRange(r.range_start, r.range_end)} · v{r.version} ·{" "}
                  {r.status}
                </option>
              ))}
            </select>
          </label>
        </div>

        {detail ? (
          <dl className="grid gap-3 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs text-muted-foreground">Window</dt>
              <dd className="mt-0.5 font-medium">
                {formatDateRange(detail.range_start, detail.range_end)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Generated</dt>
              <dd className="mt-0.5 font-medium">
                {formatTimestamp(detail.generated_at)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Status</dt>
              <dd className={cn("mt-0.5 font-medium capitalize", statusTone(detail.status))}>
                {detail.status}
                <span className="ml-2 font-normal text-muted-foreground">
                  v{detail.version}
                </span>
              </dd>
            </div>
          </dl>
        ) : null}

        {detail?.error_message ? (
          <p className="text-sm text-destructive">{detail.error_message}</p>
        ) : null}
        {error && detail ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : null}
      </header>

      {detailLoading && !detail ? (
        <p className="text-sm text-muted-foreground">Loading report metrics…</p>
      ) : null}

      {detail && detail.status === "failed" && Object.keys(detail.metrics).length === 0 ? (
        <p className="mb-8 text-sm text-muted-foreground">
          This report version failed before metrics were written. Choose another
          version or regenerate.
        </p>
      ) : null}

      {detail ? (
        <div className="space-y-10">
          {REPORT_SECTIONS.map((section) => (
            <section key={section.id} aria-labelledby={`section-${section.id}`}>
              <div className="mb-4 max-w-2xl">
                <h2
                  id={`section-${section.id}`}
                  className="text-lg font-semibold tracking-tight"
                >
                  {section.title}
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  {section.description}
                </p>
              </div>

              <div className="grid gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-2 lg:grid-cols-3">
                {section.id === "rework" ? (
                  <MetricFigureButton
                    label="Rework rate"
                    valueLabel={formatMetricValue(rate, "rate")}
                    hint={
                      rate == null
                        ? "Needs reopen count and completed issues in this report"
                        : "Reopens ÷ completed issues · evidence from reopens"
                    }
                    disabled={rate == null}
                    onClick={() =>
                      openEvidence(
                        METRIC_KEYS.REOPEN_EVENT_COUNT,
                        "Rework rate (reopen evidence)",
                        "count",
                      )
                    }
                  />
                ) : null}
                {section.figures.map((figure) => {
                  const value = metricValue(detail.metrics, figure.metricKey);
                  const missing = !(figure.metricKey in detail.metrics);
                  return (
                    <MetricFigureButton
                      key={figure.metricKey}
                      label={figure.label}
                      valueLabel={formatMetricValue(value, figure.format)}
                      hint={
                        missing
                          ? "Metric not present on this report"
                          : figure.hint
                      }
                      disabled={missing}
                      onClick={() =>
                        openEvidence(
                          figure.metricKey,
                          figure.label,
                          figure.format,
                        )
                      }
                    />
                  );
                })}
              </div>
            </section>
          ))}

          <section aria-labelledby="section-trends">
            <div className="mb-4 max-w-2xl">
              <h2
                id="section-trends"
                className="text-lg font-semibold tracking-tight"
              >
                Trends
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {trends.length > 0
                  ? `Across ${rangeDetails.length} report versions for this date window.`
                  : "Only one usable snapshot for this date window — trends appear when additional versions exist for the same range."}
              </p>
            </div>
            {trends.length > 0 ? (
              <div className="grid gap-6 sm:grid-cols-2">
                {trends.map((series) => (
                  <div
                    key={series.metricKey}
                    className="border-t border-border pt-4"
                  >
                    <p className="text-sm font-medium">{series.label}</p>
                    <TrendSparkline
                      className="mt-2"
                      points={series.points}
                      formatValue={(v) => formatMetricValue(v, series.format)}
                    />
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Snapshot only — no multi-version series to chart yet.
              </p>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}

function MetricFigureButton({
  label,
  valueLabel,
  hint,
  disabled,
  onClick,
}: {
  label: string;
  valueLabel: string;
  hint?: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex flex-col items-start gap-1 bg-background px-4 py-5 text-left transition-colors",
        "hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none",
        "disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-background",
      )}
    >
      <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      <span className="text-3xl font-semibold tracking-tight tabular-nums">
        {valueLabel}
      </span>
      {hint ? (
        <span className="text-[11px] text-muted-foreground">{hint}</span>
      ) : (
        <span className="text-[11px] text-muted-foreground">
          Click for evidence
        </span>
      )}
    </button>
  );
}
