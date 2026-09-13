/**
 * Light smoke checks for the web shell (issue #24).
 * Run via: npm run test:smoke
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const nav = readFileSync(join(root, "src/lib/nav.ts"), "utf8");
for (const href of [
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
