"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import { clearSession, getUser, type SessionUser } from "../lib/api";

export default function AppBar() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [mounted, setMounted] = useState(false);
  const pathname = usePathname();
  const router = useRouter();

  // Read session on mount + whenever route changes (login/logout reflect quickly).
  useEffect(() => {
    setMounted(true);
    setUser(getUser());
  }, [pathname]);

  function logout() {
    clearSession();
    setUser(null);
    router.push("/login");
  }

  const navLink = (href: string, label: string) => {
    const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
    return (
      <Link
        href={href}
        className={`nav-link${active ? " nav-link-active" : ""}`}
        // `aria-current="page"` is how a screen reader announces WHICH
        // destination you are on. Without it the current item was conveyed by
        // colour and background alone -- information that does not reach a
        // reader who cannot see it, on the one control that says where you are.
        aria-current={active ? "page" : undefined}
      >
        {label}
      </Link>
    );
  };

  // UI-only affordance: nav is built from the session role purely to shape what
  // each role SEES. It does NOT restrict access — every route is reachable by
  // URL. What restricts anything is server-side: money movement is csr/admin at
  // the gateway, principal-verified at servicing and needs a second approver
  // (`docs/DEBT.md` D8, closed), and a borrower's loan reads are
  // ownership-checked. *This comment claimed the API accepts ANY authenticated
  // caller while also citing D8 as fixed; both could not be current.*
  // EXCEPTION worth naming here: "Policy Chat" below is backed by a real
  // server-side gate — the gateway's /assistant/* proxy only forwards to
  // loan-assistant for csr/underwriter/admin sessions (gateway/app/main.py
  // assistant()) — so unlike every other link here, hiding/showing this one
  // matches what the backend actually enforces, not just what's convenient to
  // click.
  const navItems = ((): { href: string; label: string }[] => {
    switch (user?.role) {
      case "borrower":
        return [
          { href: "/", label: "Home" },
          { href: "/apply", label: "Apply" },
          { href: "/my-loan", label: "My Loan" },
        ];
      // "Reconciliation" is on all three staff roles because the in-app queue
      // is the ONLY place a payment flagged for review is reported -- the client
      // ruled out email, Slack, PagerDuty, webhooks and SMS before the freeze --
      // and any staff principal may read it (visibility is not authority, the
      // same rule the Approvals queue follows). A destination nobody can
      // navigate to is not a destination.
      case "csr":
        return [
          { href: "/", label: "Home" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
          { href: "/reconciliation", label: "Reconciliation" },
          { href: "/policy-chat", label: "Policy Chat" },
        ];
      case "underwriter":
        return [
          { href: "/", label: "Home" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
          { href: "/reconciliation", label: "Reconciliation" },
          { href: "/policy-chat", label: "Policy Chat" },
        ];
      case "admin":
        return [
          { href: "/", label: "Home" },
          { href: "/admin", label: "Overview" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
          { href: "/reconciliation", label: "Reconciliation" },
          { href: "/policy-chat", label: "Policy Chat" },
        ];
      default:
        // anonymous / unknown role
        return [
          { href: "/", label: "Home" },
          { href: "/apply", label: "Apply" },
        ];
    }
  })();

  return (
    <header className="appbar">
      <div className="appbar-inner">
        <Link href="/" className="wordmark">
          <span className="wordmark-mark" aria-hidden>
            ◆
          </span>
          <span className="wordmark-text">
            Meridian<span className="wordmark-thin"> Lending</span>
          </span>
        </Link>

        {/* Labelled because a page can carry more than one nav landmark, and a
            screen-reader user choosing between them needs them named. The links
            were each wrapped in a bare <span> that carried no styling and made
            every link a flex item's only child, which is one more box between
            the flex container and the thing whose wrapping it is trying to
            control. */}
        <nav className="appbar-nav" aria-label="Primary navigation">
          {navItems.map((item) => (
            <Fragment key={item.href}>{navLink(item.href, item.label)}</Fragment>
          ))}
        </nav>

        <div className="appbar-auth">
          {/* Avoid hydration mismatch: render auth state only after mount. */}
          {!mounted ? null : user ? (
            <>
              <span className="auth-user">
                {/* One string, kept whole. `users.display_name` is a single
                    column -- "Dana Whitfield (VP Lending Ops)" is not a name and
                    a title, it is a name, and there is no title field to read.
                    So it is not split on its bracket: that would be the UI
                    inventing structure from punctuation. It is allowed to
                    ellipsis at very narrow widths rather than wrap mid-name. */}
                <span className="auth-name">{user.name || user.username}</span>
                <span className="auth-role">{user.role}</span>
              </span>
              <button className="btn-ghost btn-sm" onClick={logout}>
                Log out
              </button>
            </>
          ) : (
            <Link href="/login" className="btn-ghost btn-sm">
              Log in
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
