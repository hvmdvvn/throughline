"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  getReportMetricEvidence,
  type DiagnosticMetricEvidencePage,
} from "@/lib/api";
import { evidenceDisplayParts, formatMetricValue } from "@/lib/report-metrics";
import type { MetricFormat } from "@/lib/report-metrics";

const PAGE_SIZE = 50;

export type EvidenceTarget = {
  reportId: string;
  metricKey: string;
  label: string;
  format: MetricFormat;
};

export function EvidencePanel({
  target,
  onClose,
}: {
  target: EvidenceTarget | null;
  onClose: () => void;
}) {
  const { getToken } = useAuth();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DiagnosticMetricEvidencePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (nextPage: number) => {
      if (!target) return;
      setLoading(true);
      setError(null);
      try {
        const token = await getToken();
        if (!token) {
          throw new Error("No Clerk session token available.");
        }
        const result = await getReportMetricEvidence(
          token,
          target.reportId,
          target.metricKey,
          nextPage,
          PAGE_SIZE,
        );
        setData(result);
        setPage(nextPage);
      } catch (err) {
        const message =
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "Failed to load evidence";
        setError(message);
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    [getToken, target],
  );

  useEffect(() => {
    if (!target) {
      setData(null);
      setError(null);
      setPage(1);
      return;
    }
    void load(1);
  }, [target, load]);

  if (!target) {
    return null;
  }

  const totalPages =
    data && data.limit > 0 ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;

  return (
    <aside
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-border bg-background shadow-lg"
      aria-label={`Evidence for ${target.label}`}
      role="dialog"
      aria-modal="true"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Evidence
          </p>
          <h2 className="mt-1 truncate text-lg font-semibold tracking-tight">
            {target.label}
          </h2>
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            {target.metricKey}
          </p>
          {data ? (
            <p className="mt-2 text-sm text-muted-foreground">
              Value{" "}
              <span className="font-medium text-foreground">
                {formatMetricValue(data.value, target.format)}
              </span>
              <span className="mx-1.5 text-border">·</span>
              {data.total.toLocaleString()} issue
              {data.total === 1 ? "" : "s"}
            </p>
          ) : null}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          aria-label="Close evidence panel"
        >
          <X />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {loading && !data ? (
          <p className="text-sm text-muted-foreground">Loading evidence…</p>
        ) : null}
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : null}
        {data && data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No linked issues for this figure.
          </p>
        ) : null}
        {data && data.items.length > 0 ? (
          <ul className="divide-y divide-border">
            {data.items.map((ref) => {
              const { primary, detail } = evidenceDisplayParts(ref);
              return (
                <li key={ref} className="py-3">
                  <p className="font-mono text-sm font-medium tracking-tight">
                    {primary}
                  </p>
                  {detail ? (
                    <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>
                  ) : null}
                  <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground/80">
                    {ref}
                  </p>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>

      {data && data.total > PAGE_SIZE ? (
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loading || page <= 1}
            onClick={() => void load(page - 1)}
          >
            Previous
          </Button>
          <span className="text-xs text-muted-foreground">
            Page {page} of {totalPages}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loading || !data.has_more}
            onClick={() => void load(page + 1)}
          >
            Next
          </Button>
        </div>
      ) : null}
    </aside>
  );
}
