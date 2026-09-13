/**
 * FastAPI client helpers (issue #24).
 *
 * Browser and server call Throughline FastAPI only — not Django.
 * Authenticated routes send Clerk session JWT as ``Authorization: Bearer``.
 */

export function getApiBaseUrl(): string {
  const base = process.env.NEXT_PUBLIC_API_URL?.trim();
  return (base && base.length > 0 ? base : "http://localhost:8000").replace(
    /\/$/,
    "",
  );
}

export type ApiFetchOptions = RequestInit & {
  /** Clerk session JWT; omit for public endpoints like ``/health``. */
  token?: string | null;
  /** Optional org override when the user has multiple memberships. */
  orgId?: string | null;
};

export async function apiFetch(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  const { token, orgId, headers, ...rest } = options;
  const merged = new Headers(headers);
  if (!merged.has("Accept")) {
    merged.set("Accept", "application/json");
  }
  if (token) {
    merged.set("Authorization", `Bearer ${token}`);
  }
  if (orgId) {
    merged.set("X-Org-Id", orgId);
  }
  const url = path.startsWith("http")
    ? path
    : `${getApiBaseUrl()}${path.startsWith("/") ? path : `/${path}`}`;
  return fetch(url, { ...rest, headers: merged });
}

export type MeResponse = {
  user_id: string;
  auth_subject: string;
  email: string | null;
  display_name: string | null;
  org_id: string;
  membership_id: string;
  role: string;
};

/** Diagnostic report list row (``GET /reports`` — issue #23). */
export type DiagnosticReportListItem = {
  id: string;
  org_id: string;
  range_start: string;
  range_end: string;
  version: number;
  status: string;
  generated_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type DiagnosticMetricEntry = {
  value: number | null;
  evidence_refs: string[];
};

/** Full report payload (``GET /reports/{id}``). */
export type DiagnosticReportDetail = {
  id: string;
  org_id: string;
  range_start: string;
  range_end: string;
  version: number;
  status: string;
  metrics: Record<string, DiagnosticMetricEntry>;
  generation_detail: Record<string, unknown>;
  generated_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

/** Paginated evidence (``GET /reports/{id}/metrics/{metric_key}/evidence``). */
export type DiagnosticMetricEvidencePage = {
  report_id: string;
  metric_key: string;
  value: number | null;
  items: string[];
  page: number;
  limit: number;
  total: number;
  has_more: boolean;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseJsonOrThrow<T>(res: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail =
      body &&
      typeof body === "object" &&
      "detail" in body &&
      (body as { detail?: unknown }).detail != null
        ? String((body as { detail: unknown }).detail)
        : res.statusText || "Request failed";
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export async function listReports(
  token: string,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticReportListItem[]> {
  const res = await apiFetch("/reports", { ...options, token, cache: "no-store" });
  return parseJsonOrThrow<DiagnosticReportListItem[]>(res);
}

export async function getReport(
  token: string,
  reportId: string,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticReportDetail> {
  const res = await apiFetch(`/reports/${encodeURIComponent(reportId)}`, {
    ...options,
    token,
    cache: "no-store",
  });
  return parseJsonOrThrow<DiagnosticReportDetail>(res);
}

export async function getReportMetricEvidence(
  token: string,
  reportId: string,
  metricKey: string,
  page = 1,
  limit = 50,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticMetricEvidencePage> {
  const params = new URLSearchParams({
    page: String(page),
    limit: String(limit),
  });
  const path =
    `/reports/${encodeURIComponent(reportId)}` +
    `/metrics/${encodeURIComponent(metricKey)}/evidence?${params}`;
  const res = await apiFetch(path, { ...options, token, cache: "no-store" });
  return parseJsonOrThrow<DiagnosticMetricEvidencePage>(res);
}

/** Guided diagnostic onboarding progress (issue #27). */
export type DiagnosticOnboardingProgress = {
  id: string;
  org_id: string;
  stage: string;
  notify_email: string;
  range_start: string;
  range_end: string;
  jql: string | null;
  progress_status: string | null;
  progress_imported_count: number;
  progress_total_estimate: number | null;
  progress_detail: string | null;
  progress_updated_at: string | null;
  orchestrator_job_id: string | null;
  report_id: string | null;
  email_status: string | null;
  email_detail: string | null;
  email_sent_at: string | null;
  failed_stage: string | null;
  error_message: string | null;
  completed_at: string | null;
  authorize_url: string | null;
  report_url: string | null;
  created_at: string;
  updated_at: string;
  recoverable: boolean;
};

export type DiagnosticOnboardingStartBody = {
  notify_email: string;
  range_start: string;
  range_end: string;
  jql?: string | null;
};

export async function getDiagnosticOnboarding(
  token: string,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticOnboardingProgress> {
  const res = await apiFetch("/onboarding/diagnostic", {
    ...options,
    token,
    cache: "no-store",
  });
  return parseJsonOrThrow<DiagnosticOnboardingProgress>(res);
}

export async function startDiagnosticOnboarding(
  token: string,
  body: DiagnosticOnboardingStartBody,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticOnboardingProgress> {
  const res = await apiFetch("/onboarding/diagnostic", {
    ...options,
    token,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJsonOrThrow<DiagnosticOnboardingProgress>(res);
}

export async function continueDiagnosticOnboarding(
  token: string,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticOnboardingProgress> {
  const res = await apiFetch("/onboarding/diagnostic/continue", {
    ...options,
    token,
    method: "POST",
  });
  return parseJsonOrThrow<DiagnosticOnboardingProgress>(res);
}

export async function retryDiagnosticOnboarding(
  token: string,
  options: Omit<ApiFetchOptions, "token"> = {},
): Promise<DiagnosticOnboardingProgress> {
  const res = await apiFetch("/onboarding/diagnostic/retry", {
    ...options,
    token,
    method: "POST",
  });
  return parseJsonOrThrow<DiagnosticOnboardingProgress>(res);
}
