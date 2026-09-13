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
