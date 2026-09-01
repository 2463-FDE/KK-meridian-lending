"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, clearSession, getToken, getUser, roleHome, setSession } from "../lib/api";

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
 * UX-layer only. It stops a logged-in borrower from *landing* on a staff page by
 * URL; it is not the access control. The controls that matter are server-side
 * and they exist: money movement is csr/admin at the gateway, principal-verified
 * at servicing, and gated on a second approver (`docs/DEBT.md` D8, closed);
 * borrower loan reads are ownership-checked. Routes with nothing sensitive
 * behind them remain reachable by any authenticated caller. *This comment cited
 * D8 as the reason "the gateway/API still accept any authenticated caller",
 * which stopped being true of the money routes in PRs #33-#35.*
 */
/**
 * The role `/auth/me` returned for THIS page load.
 *
 * Codex review of PR #152, MDTI-UI-01. This guard already fetched the verified
 * role and then threw it away, so anything downstream that needed to know who
 * was looking fell back to `getUser()` -- the cached `localStorage` copy, which
 * a user can edit and which goes stale on its own (a role changed server-side
 * outlives the cache). A panel gated on the cache is wrong in both directions:
 * it shows a form to someone whose every request will be refused, and it hides
 * one from someone genuinely entitled to it.
 *
 * `null` means the role is not known yet, which is not the same as "not
 * permitted" -- a consumer must not treat it as a refusal, because this guard
 * renders nothing until the check completes.
 */
const VerifiedRoleContext = createContext<string | null>(null);

/** The verified role for the current page, or `null` before it is known. */
export function useVerifiedRole(): string | null {
  return useContext(VerifiedRoleContext);
}

export default function RequireRole({
  allow,
  children,
}: {
  allow: string[];
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<"checking" | "ok">("checking");
  const [verifiedRole, setVerifiedRole] = useState<string | null>(null);

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
      // Reconcile the cache with what the server just said. Without this the
      // stale copy survives the page load and every OTHER consumer of
      // `getUser()` keeps reading it -- the nav's role chip among them, so the
      // screen would disagree with itself about who is looking.
      const token = getToken();
      if (cached.role !== role && token) {
        setSession(token, { ...cached, role });
      }
      setVerifiedRole(role);
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

  return (
    <VerifiedRoleContext.Provider value={verifiedRole}>
      {children}
    </VerifiedRoleContext.Provider>
  );
}
