"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button, buttonVariants } from "@/components/ui/button";
import {
  ApiError,
  continueDiagnosticOnboarding,
  getDiagnosticOnboarding,
  retryDiagnosticOnboarding,
  startDiagnosticOnboarding,
  type DiagnosticOnboardingProgress,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const STAGES = [
  { key: "awaiting_oauth", label: "Connect Jira" },
  { key: "importing_issues", label: "Import issues" },
  { key: "importing_changelog", label: "Import changelog" },
  { key: "generating_report", label: "Generate report" },
  { key: "sending_email", label: "Email when ready" },
  { key: "completed", label: "Done" },
] as const;

const POLL_MS = 2500;

function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date();
  start.setUTCMonth(start.getUTCMonth() - 3);
  return {
    start: start.toISOString().slice(0, 10),
    end: end.toISOString().slice(0, 10),
  };
}

function stageIndex(stage: string): number {
  if (stage === "failed") return -1;
  const idx = STAGES.findIndex((s) => s.key === stage);
  return idx >= 0 ? idx : 0;
}

export function OnboardingWizard() {
  const { getToken, isLoaded, isSignedIn } = useAuth();
  const searchParams = useSearchParams();
  const rangeDefaults = useMemo(() => defaultRange(), []);

  const [email, setEmail] = useState("");
  const [rangeStart, setRangeStart] = useState(rangeDefaults.start);
  const [rangeEnd, setRangeEnd] = useState(rangeDefaults.end);
  const [progress, setProgress] = useState<DiagnosticOnboardingProgress | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!isSignedIn) return;
    const token = await getToken();
    if (!token) return;
    try {
      const next = await getDiagnosticOnboarding(token);
      setProgress(next);
      setError(null);
      if (!email && next.notify_email) setEmail(next.notify_email);
      if (next.range_start) setRangeStart(next.range_start);
      if (next.range_end) setRangeEnd(next.range_end);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setProgress(null);
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load onboarding");
    }
  }, [email, getToken, isSignedIn]);

  useEffect(() => {
    if (!isLoaded) return;
    if (!isSignedIn) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoading(true);
      await refresh();
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [isLoaded, isSignedIn, refresh]);

  // Auto-continue after OAuth return (?jira=connected).
  useEffect(() => {
    if (!isSignedIn || !progress) return;
    if (searchParams.get("jira") !== "connected") return;
    if (progress.stage !== "awaiting_oauth") return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        const token = await getToken();
        if (!token || cancelled) return;
        const next = await continueDiagnosticOnboarding(token);
        if (!cancelled) setProgress(next);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Could not continue after OAuth",
          );
        }
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [getToken, isSignedIn, progress, searchParams]);

  // Poll while long-running stages are active (leave/return safe).
  useEffect(() => {
    if (!progress) return;
    const running = [
      "importing_issues",
      "importing_changelog",
      "generating_report",
      "sending_email",
    ].includes(progress.stage);
    if (!running) return;
    const id = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [progress, refresh]);

  async function onStart(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      if (!token) throw new Error("Missing session token");
      const next = await startDiagnosticOnboarding(token, {
        notify_email: email.trim(),
        range_start: rangeStart,
        range_end: rangeEnd,
      });
      setProgress(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start onboarding");
    } finally {
      setBusy(false);
    }
  }

  async function onContinue() {
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      if (!token) throw new Error("Missing session token");
      const next = await continueDiagnosticOnboarding(token);
      setProgress(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not continue");
    } finally {
      setBusy(false);
    }
  }

  async function onRetry() {
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      if (!token) throw new Error("Missing session token");
      const next = await retryDiagnosticOnboarding(token);
      setProgress(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry");
    } finally {
      setBusy(false);
    }
  }

  if (!isLoaded || loading) {
    return (
      <p className="text-sm text-muted-foreground">Loading onboarding…</p>
    );
  }

  if (!isSignedIn) {
    return (
      <p className="text-sm text-muted-foreground">
        Sign in to start the diagnostic onboarding flow.
      </p>
    );
  }

  const currentIdx = progress ? stageIndex(progress.stage) : -1;
  const oauthError = searchParams.get("jira") === "error";

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          Diagnostic onboarding
        </h1>
        <p className="text-sm text-muted-foreground">
          Connect Jira, import history, and generate an evidence-linked report.
          You can leave this page — imports keep running and we email you when
          the report is ready.
        </p>
      </header>

      <ol className="space-y-2">
        {STAGES.map((step, idx) => {
          const active = currentIdx === idx;
          const done =
            progress?.stage === "completed" ||
            (currentIdx > idx && progress?.stage !== "failed");
          return (
            <li
              key={step.key}
              className={cn(
                "flex items-center gap-3 text-sm",
                active && "font-medium text-foreground",
                done && "text-muted-foreground",
                !active && !done && "text-muted-foreground/70",
              )}
            >
              <span
                className={cn(
                  "flex h-6 w-6 items-center justify-center rounded-full border text-xs",
                  active && "border-foreground bg-foreground text-background",
                  done && "border-muted-foreground/40",
                )}
              >
                {done ? "✓" : idx + 1}
              </span>
              {step.label}
            </li>
          );
        })}
      </ol>

      {(error || oauthError) && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error ||
            searchParams.get("detail") ||
            "Jira authorization failed. Try connecting again."}
        </div>
      )}

      {progress?.stage === "failed" && (
        <div className="space-y-3 rounded-md border border-destructive/30 px-4 py-3">
          <p className="text-sm font-medium text-destructive">
            Onboarding failed
            {progress.failed_stage ? ` at ${progress.failed_stage}` : ""}
          </p>
          <p className="text-sm text-muted-foreground">
            {progress.error_message || "Unknown error"}
          </p>
          <Button type="button" onClick={() => void onRetry()} disabled={busy}>
            Retry from failed stage
          </Button>
        </div>
      )}

      {progress?.stage === "completed" && (
        <div className="space-y-3 rounded-md border px-4 py-3">
          <p className="text-sm font-medium">Report ready</p>
          <p className="text-sm text-muted-foreground">
            Email status: {progress.email_status ?? "unknown"}
            {progress.email_detail ? ` — ${progress.email_detail}` : ""}
          </p>
          {progress.report_id && (
            <Link
              href={`/diagnostic?report=${progress.report_id}`}
              className={buttonVariants()}
            >
              Open diagnostic report
            </Link>
          )}
        </div>
      )}

      {progress?.stage === "awaiting_oauth" && (
        <div className="space-y-3 rounded-md border px-4 py-3">
          <p className="text-sm">
            Connect your Jira Cloud site to continue. After consent you will
            return here automatically.
          </p>
          {progress.authorize_url ? (
            <a href={progress.authorize_url} className={buttonVariants()}>
              Connect Jira
            </a>
          ) : (
            <p className="text-sm text-muted-foreground">
              Atlassian OAuth is not configured on the API (set
              ATLASSIAN_CLIENT_ID / SECRET / REDIRECT_URI).
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            onClick={() => void onContinue()}
            disabled={busy}
          >
            I already connected — continue
          </Button>
        </div>
      )}

      {progress &&
        [
          "importing_issues",
          "importing_changelog",
          "generating_report",
          "sending_email",
        ].includes(progress.stage) && (
          <div className="space-y-2 rounded-md border px-4 py-3">
            <p className="text-sm font-medium">In progress: {progress.stage}</p>
            <p className="text-sm text-muted-foreground">
              {progress.progress_detail ||
                "Working… you can leave and come back."}
            </p>
            {(progress.stage === "importing_issues" ||
              progress.stage === "importing_changelog") && (
              <p className="text-sm tabular-nums text-muted-foreground">
                {progress.progress_imported_count}
                {progress.progress_total_estimate != null
                  ? ` / ${progress.progress_total_estimate}`
                  : ""}{" "}
                · status {progress.progress_status ?? "—"}
              </p>
            )}
            {progress.orchestrator_job_id && (
              <p className="text-xs text-muted-foreground">
                Job {progress.orchestrator_job_id}
              </p>
            )}
          </div>
        )}

      {(!progress ||
        progress.stage === "completed" ||
        progress.stage === "failed") && (
        <form onSubmit={(e) => void onStart(e)} className="space-y-4">
          <div className="space-y-1">
            <label htmlFor="notify_email" className="text-sm font-medium">
              Email when ready
            </label>
            <input
              id="notify_email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              placeholder="you@company.com"
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor="range_start" className="text-sm font-medium">
                Range start
              </label>
              <input
                id="range_start"
                type="date"
                required
                value={rangeStart}
                onChange={(e) => setRangeStart(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="range_end" className="text-sm font-medium">
                Range end
              </label>
              <input
                id="range_end"
                type="date"
                required
                value={rangeEnd}
                onChange={(e) => setRangeEnd(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </div>
          </div>
          <Button type="submit" disabled={busy}>
            {progress ? "Restart onboarding" : "Start diagnostic onboarding"}
          </Button>
        </form>
      )}
    </div>
  );
}
