"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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
      <Link href={href} className={`nav-link${active ? " nav-link-active" : ""}`}>
        {label}
      </Link>
    );
  };

  // UI-only affordance: nav is built from the session role purely to shape what
  // each role SEES. It does NOT restrict access — every route is reachable by
  // URL and the gateway/API still accept ANY authenticated caller (debt D8,
  // fixed in W6). EXCEPTION: "Policy Chat" below actually is backed by a real
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
      case "csr":
        return [
          { href: "/", label: "Home" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
          { href: "/policy-chat", label: "Policy Chat" },
        ];
      case "underwriter":
        return [
          { href: "/", label: "Home" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
          { href: "/policy-chat", label: "Policy Chat" },
        ];
      case "admin":
        return [
          { href: "/", label: "Home" },
          { href: "/admin", label: "Overview" },
          { href: "/underwriting", label: "Underwriting" },
          { href: "/servicing", label: "Servicing" },
          { href: "/approvals", label: "Approvals" },
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

        <nav className="appbar-nav">
          {navItems.map((item) => (
            <span key={item.href}>{navLink(item.href, item.label)}</span>
          ))}
        </nav>

        <div className="appbar-auth">
          {/* Avoid hydration mismatch: render auth state only after mount. */}
          {!mounted ? null : user ? (
            <>
              <span className="auth-user">
                {user.name || user.username}
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
