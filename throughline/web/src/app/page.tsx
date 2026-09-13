import { SignInButton } from "@clerk/nextjs";
import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";

import { Button } from "@/components/ui/button";

export default async function HomePage() {
  const { userId } = await auth();
  if (userId) {
    redirect("/diagnostic");
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 px-6">
      <div className="text-center">
        <p className="text-sm font-medium tracking-wide text-muted-foreground">
          Throughline
        </p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Sign in to open the app shell
        </h1>
        <p className="mt-2 max-w-md text-sm text-muted-foreground">
          Hosted auth is Clerk (same provider as the FastAPI API). Feature
          report UI is issue #25 — this shell only proves auth and navigation.
        </p>
      </div>
      <SignInButton mode="redirect" forceRedirectUrl="/diagnostic">
        <Button type="button">Sign in</Button>
      </SignInButton>
    </div>
  );
}
