import { UserButton } from "@clerk/nextjs";

import { AppNav } from "@/components/app-nav";
import { Separator } from "@/components/ui/separator";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="flex w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground">
        <div className="flex items-center justify-between px-4 py-4">
          <span className="text-sm font-semibold tracking-tight">
            Throughline
          </span>
          <UserButton />
        </div>
        <Separator />
        <div className="flex-1 px-2 py-3">
          <AppNav />
        </div>
        <p className="px-4 pb-4 text-xs text-muted-foreground">
          Shell placeholders — feature UIs land in later issues.
        </p>
      </aside>
      <main className="flex-1 overflow-auto p-6 md:p-8">{children}</main>
    </div>
  );
}
