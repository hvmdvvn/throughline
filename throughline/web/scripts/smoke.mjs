/**
 * Light smoke checks for the web shell (issue #24) and diagnostic UI (#25).
 * Run via: npm run test:smoke
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const nav = readFileSync(join(root, "src/lib/nav.ts"), "utf8");
for (const href of [
  "/onboarding",
  "/diagnostic",
  "/pipeline",
  "/needs-attention",
  "/settings",
]) {
  assert.match(nav, new RegExp(`href:\\s*"${href}"`));
  assert.ok(
    existsSync(join(root, "src/app/(shell)", href.slice(1), "page.tsx")),
    `missing page for ${href}`,
  );
}

const proxy = readFileSync(join(root, "src/proxy.ts"), "utf8");
assert.match(proxy, /clerkMiddleware/);
assert.match(proxy, /createRouteMatcher/);
assert.match(proxy, /auth\.protect/);

const api = readFileSync(join(root, "src/lib/api.ts"), "utf8");
assert.match(api, /Authorization.*Bearer/);
assert.match(api, /NEXT_PUBLIC_API_URL/);
assert.match(api, /localhost:8000/);
assert.match(api, /listReports/);
assert.match(api, /getReportMetricEvidence/);
assert.match(api, /\/reports/);
assert.match(api, /startDiagnosticOnboarding/);
assert.match(api, /\/onboarding\/diagnostic/);

const metrics = readFileSync(join(root, "src/lib/report-metrics.ts"), "utf8");
assert.match(metrics, /reopen\.event_count/);
assert.match(metrics, /scope_change\.late_child_count/);
assert.match(metrics, /spec_quality/);
assert.match(metrics, /estimation_accuracy\.coverage/);
assert.match(metrics, /REPORT_SECTIONS/);
assert.match(metrics, /title:\s*"Rework"/);
assert.match(metrics, /title:\s*"Scope change"/);
assert.match(metrics, /title:\s*"Spec quality"/);
assert.match(metrics, /title:\s*"Estimation accuracy"/);

const diagnosticPage = readFileSync(
  join(root, "src/app/(shell)/diagnostic/page.tsx"),
  "utf8",
);
assert.match(diagnosticPage, /DiagnosticReportView/);
assert.doesNotMatch(diagnosticPage, /PlaceholderPage/);
assert.ok(
  existsSync(
    join(root, "src/components/diagnostic/diagnostic-report-view.tsx"),
  ),
);
assert.ok(existsSync(join(root, "src/components/diagnostic/evidence-panel.tsx")));
assert.ok(existsSync(join(root, "src/components/diagnostic/trend-sparkline.tsx")));

const reportView = readFileSync(
  join(root, "src/components/diagnostic/diagnostic-report-view.tsx"),
  "utf8",
);
assert.match(reportView, /useAuth/);
assert.match(reportView, /EvidencePanel/);
assert.match(reportView, /REPORT_SECTIONS/);
assert.match(reportView, /getReportMetricEvidence|openEvidence|setEvidence/);
assert.match(reportView, /Rework rate/);
assert.match(reportView, /Trends/);
assert.match(reportView, /Loading diagnostic reports/);
assert.match(reportView, /No diagnostic reports/);

const onboardingPage = readFileSync(
  join(root, "src/app/(shell)/onboarding/page.tsx"),
  "utf8",
);
assert.match(onboardingPage, /OnboardingWizard/);
assert.ok(
  existsSync(
    join(root, "src/components/onboarding/onboarding-wizard.tsx"),
  ),
);
const onboardingWizard = readFileSync(
  join(root, "src/components/onboarding/onboarding-wizard.tsx"),
  "utf8",
);
assert.match(onboardingWizard, /startDiagnosticOnboarding/);
assert.match(onboardingWizard, /continueDiagnosticOnboarding/);
assert.match(onboardingWizard, /retryDiagnosticOnboarding/);
assert.match(onboardingWizard, /importing_issues/);

const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
assert.ok(pkg.dependencies["@clerk/nextjs"], "Clerk SDK missing");
assert.ok(pkg.dependencies.next, "Next.js missing");
assert.ok(
  existsSync(join(root, "components.json")),
  "shadcn components.json missing",
);
assert.ok(existsSync(join(root, "src/components/ui/button.tsx")));
assert.ok(existsSync(join(root, "src/components/ui/separator.tsx")));

console.log("web smoke checks passed");
