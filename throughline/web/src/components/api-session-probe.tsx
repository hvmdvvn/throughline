import { auth } from "@clerk/nextjs/server";

import { apiFetch, type MeResponse } from "@/lib/api";

/**
 * Server-side smoke of authenticated FastAPI access (``GET /me``).
 * Placeholder pages use this to prove Bearer JWT wiring without feature UI.
 */
export async function ApiSessionProbe() {
  const { getToken } = await auth();
  const token = await getToken();

  if (!token) {
    return (
      <p className="text-sm text-muted-foreground">
        No Clerk session token available for the API probe.
      </p>
    );
  }

  let status: number;
  let body: MeResponse | { detail?: string } | null = null;
  try {
    const res = await apiFetch("/me", { token, cache: "no-store" });
    status = res.status;
    try {
      body = (await res.json()) as MeResponse | { detail?: string };
    } catch {
      body = null;
    }
  } catch (err) {
    return (
      <p className="text-sm text-destructive">
        FastAPI unreachable from the web app:{" "}
        {err instanceof Error ? err.message : "unknown error"}. Start the API
        at{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-xs">
          NEXT_PUBLIC_API_URL
        </code>{" "}
        (default http://localhost:8000).
      </p>
    );
  }

  if (status !== 200 || !body || !("user_id" in body)) {
    return (
      <p className="text-sm text-muted-foreground">
        Authenticated request to FastAPI{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-xs">GET /me</code>{" "}
        returned {status}
        {body && "detail" in body && body.detail
          ? `: ${body.detail}`
          : ""}. Confirm Clerk issuer/JWKS on the API matches this frontend
        instance.
      </p>
    );
  }

  return (
    <p className="text-sm text-muted-foreground">
      FastAPI session OK — org{" "}
      <code className="rounded bg-muted px-1 py-0.5 text-xs">{body.org_id}</code>
      , role {body.role}.
    </p>
  );
}
