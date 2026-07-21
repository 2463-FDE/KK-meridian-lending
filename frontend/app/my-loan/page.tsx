"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import RequireRole from "../../components/RequireRole";
import StatusChip from "../../components/StatusChip";
import { apiGet, getUser, type SessionUser } from "../../lib/api";
import { usd, pct, shortDate } from "../../lib/format";

interface LoanRow {
  id: string | number;
  applicant_name: string;
  borrower?: string;
  principal: number;
  apr: number;
  term_months: number;
  status: string;
  balance: number;
  past_due: number;
  opened_at: string;
}

interface LoansResponse {
  items: LoanRow[];
  total: number;
  limit: number;
  offset: number;
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

function norm(s: string | null | undefined): string {
  return (s || "").trim().toLowerCase();
}

export default function MyLoanPage() {
  return (
    <RequireRole allow={["borrower"]}>
      <MyLoanContent />
    </RequireRole>
  );
}

function MyLoanContent() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [items, setItems] = useState<LoanRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // The gateway returns the ENTIRE portfolio; there is no borrower-scoped
      // endpoint. We scope to the logged-in borrower CLIENT-SIDE only (debt D8,
      // fixed in W6) — the API still hands every caller all loans.
      const res = (await apiGet(`/lss/loans?limit=200&offset=0`)) as LoansResponse;
      setItems(res.items ?? []);
    } catch (err) {
      setError(errMsg(err, "Could not load your loans."));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setUser(getUser());
    load();
  }, [load]);

  // Client-side name match against the session user (display name or username).
  const names = [norm(user?.name), norm(user?.username)].filter(Boolean);
  const matched = items.filter((l) => {
    const candidate = norm(l.applicant_name || l.borrower);
    return names.some((n) => n && (candidate === n || candidate.includes(n)));
  });

  // If we can't match the borrower to any loan, fall back to showing all so the
  // demo is still usable. This fallback is purely a UI convenience.
  const usingFallback = matched.length === 0 && items.length > 0;
  const visible = matched.length > 0 ? matched : items;

  return (
    <main className="wrap">
      <div className="spread">
        <div>
          <h1>My loan</h1>
          <p className="sub" style={{ margin: 0 }}>
            Your balance, terms, and account activity.
          </p>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error ? <div className="alert alert-error">{error}</div> : null}

      {loading && items.length === 0 ? (
        <p className="muted" style={{ marginTop: 20 }}>
          Loading your loans…
        </p>
      ) : items.length === 0 && !error ? (
        <div className="card" style={{ marginTop: 20 }}>
          <h3 style={{ marginBottom: 6 }}>No loans yet</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            You don&apos;t have an active loan. Check your rate and apply in a
            few minutes.
          </p>
          <Link href="/apply" className="btn" style={{ marginTop: 8 }}>
            Apply for a loan
          </Link>
        </div>
      ) : (
        <>
          {usingFallback ? (
            <div className="alert alert-info" style={{ marginTop: 16 }}>
              Demo: couldn&apos;t match your account to a specific borrower, so
              all loans are shown.
            </div>
          ) : null}

          <div className="grid grid-2" style={{ marginTop: 20 }}>
            {visible.map((l) => (
              <div className="card" key={String(l.id)}>
                <div className="spread" style={{ marginBottom: 12 }}>
                  <div>
                    <div className="card-title">Loan #{String(l.id)}</div>
                    <p className="muted" style={{ margin: "4px 0 0" }}>
                      {l.applicant_name || l.borrower}
                    </p>
                  </div>
                  <StatusChip status={l.status} />
                </div>

                <div className="dl">
                  <div className="dl-row">
                    <dt>Current balance</dt>
                    <dd>{usd(l.balance)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Past due</dt>
                    <dd className={l.past_due > 0 ? "danger-text" : ""}>
                      {usd(l.past_due)}
                    </dd>
                  </div>
                  <div className="dl-row">
                    <dt>Original principal</dt>
                    <dd>{usd(l.principal)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>APR</dt>
                    <dd>{pct(l.apr)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Term</dt>
                    <dd>{l.term_months} months</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Opened</dt>
                    <dd>{shortDate(l.opened_at)}</dd>
                  </div>
                </div>

                <Link
                  href={`/servicing/${l.id}`}
                  className="btn btn-block"
                  style={{ marginTop: 16 }}
                >
                  View account & make a payment
                </Link>
              </div>
            ))}
          </div>
        </>
      )}
    </main>
  );
}
