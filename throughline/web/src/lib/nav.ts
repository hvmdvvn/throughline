/**
 * App-shell navigation placeholders (issue #24).
 *
 * TASKS.md task 24 does not name the four destinations. Choice for Phase 0/1:
 * Diagnostic report (#25), Pipeline (Phase 1 board), Needs attention (Phase 1
 * queue), Settings (connections / field mappings / prefs).
 */
export const NAV_ITEMS = [
  { href: "/diagnostic", label: "Diagnostic report" },
  { href: "/pipeline", label: "Pipeline" },
  { href: "/needs-attention", label: "Needs attention" },
  { href: "/settings", label: "Settings" },
] as const;
