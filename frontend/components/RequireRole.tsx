"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, clearSession, getUser, roleHome } from "../lib/api";

/**
 * Client-side route guard: redirects to /login (no session) or the caller's
 * own role home (wrong role) before rendering `children`.
 *
 * Validates the cached session against the server (GET /auth/me) rather than
 * trusting localStorage alone -- a cached user object can outlive its actual
 * server-side session (Redis session TTL expiry, session store restart, an
 * admin revoking it), which used to let this guard through on a token the
 * next real API call would reject with 401. That surfaced as a confusing
 * "not authenticated" error deep inside a feature instead of a login prompt.
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
    let cancelled = false;
    (async () => {
      const cached = getUser();
      if (!cached) {
        router.replace("/login");
        return;
      }
      let role: string;
      try {
        const me = (await apiGet("/auth/me")) as { role: string };
        role = me.role;
      } catch {
        clearSession();
        router.replace("/login");
        return;
      }
      if (cancelled) return;
      if (!allow.includes(role)) {
        router.replace(roleHome(role));
        return;
      }
      setStatus("ok");
    })();
    // Only re-check on mount -- this guard wraps a page.tsx, which already
    // remounts on route-segment changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => {
      cancelled = true;
    };
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
