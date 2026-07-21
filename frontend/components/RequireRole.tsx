"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getUser, roleHome } from "../lib/api";

/**
 * Client-side route guard: redirects to /login (no session) or the caller's
 * own role home (wrong role) before rendering `children`.
 *
 * UX-layer only -- the gateway/API still accept any authenticated caller on
 * most routes (debt D8, see lib/api.ts::roleHome / AppBar.tsx). This stops a
 * logged-in borrower from *landing* on a staff page by URL; it is not a
 * substitute for the server-side authz fix.
 */
export default function RequireRole({
  allow,
  children,
}: {
  allow: string[];
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<"checking" | "ok">("checking");

  useEffect(() => {
    const user = getUser();
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!allow.includes(user.role)) {
      router.replace(roleHome(user.role));
      return;
    }
    setStatus("ok");
    // Only re-check on mount -- this guard wraps a page.tsx, which already
    // remounts on route-segment changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (status !== "ok") {
    return (
      <main className="wrap">
        <p className="muted">Checking access…</p>
      </main>
    );
  }

  return <>{children}</>;
}
