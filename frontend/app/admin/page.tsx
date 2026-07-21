"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import RequireRole from "../../components/RequireRole";
import StatusChip from "../../components/StatusChip";
import { apiGet } from "../../lib/api";
import { usd, shortDate } from "../../lib/format";

interface AppRow {
  id: string | number;
  applicant_name: string;
  amount: number;
  term_months: number;
  purpose: string;
  status: string;
  created_at: string;
}

interface LoanRow {
  id: string | number;
  applicant_name: string;
  status: string;
  balance: number;
  past_due: number;
}

interface Paged<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

const PAGE = 50;

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function AdminOverviewPage() {
  return (
    <RequireRole allow={["admin"]}>
      <AdminOverviewContent />
    </RequireRole>
  );
}

function AdminOverviewContent() {
  const [apps, setApps] = useState<Paged<AppRow> | null>(null);
  const [loans, setLoans] = useState<Paged<LoanRow> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [a, l] = await Promise.all([
        apiGet(`/los/applications?limit=${PAGE}&offset=0`),
        apiGet(`/lss/loans?limit=${PAGE}&offset=0`),
      ]);
      setApps(a as Paged<AppRow>);
      setLoans(l as Paged<LoanRow>);
    } catch (err) {
      setError(errMsg(err, "Could not load the portfolio overview."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const appItems = apps?.items ?? [];
  const loanItems = loans?.items ?? [];

  // Application counts by status (from the loaded page).
  const appStatus = appItems.reduce<Record<string, number>>((acc, a) => {
    const k = (a.status || "unknown").toLowerCase();
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});

  const portfolioBalance = loanItems.reduce(
    (sum, l) => sum + (l.balance || 0),
    0
  );
  const delinquentCount = loanItems.filter((l) =>
    ["delinquent", "past_due"].includes((l.status || "").toLowerCase())
  ).length;

  const recentApps = appItems.slice(0, 8);
  const recentLoans = loanItems.slice(0, 8);

  return (
    <main className="wrap">
      <div className="spread">
        <div>
          <h1>Portfolio overview</h1>
          <p className="sub" style={{ margin: 0 }}>
            Origination pipeline and serviced portfolio at a glance.
          </p>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error ? <div className="alert alert-error">{error}</div> : null}

      {/* Top-line KPIs */}
      <div className="grid grid-4" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Applications</div>
          <div className="kpi-value">{apps?.total ?? "—"}</div>
          <div className="kpi-sub">Total in origination</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Funded loans</div>
          <div className="kpi-value">{loans?.total ?? "—"}</div>
          <div className="kpi-sub">Total boarded to servicing</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Portfolio balance (page)</div>
          <div className="kpi-value">{usd(portfolioBalance)}</div>
          <div className="kpi-sub">Sum across loaded loans</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Delinquent (page)</div>
          <div className={`kpi-value${delinquentCount > 0 ? " danger" : ""}`}>
            {delinquentCount}
          </div>
          <div className="kpi-sub">Past-due accounts</div>
        </div>
      </div>

      {/* Application status breakdown */}
      <div className="card">
        <div className="card-title" style={{ marginBottom: 12 }}>
          Applications by status (loaded page)
        </div>
        {Object.keys(appStatus).length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            {loading ? "Loading…" : "No applications to summarize."}
          </p>
        ) : (
          <div className="row">
            {Object.entries(appStatus)
              .sort((a, b) => b[1] - a[1])
              .map(([status, count]) => (
                <span key={status} className="row" style={{ gap: 6 }}>
                  <StatusChip status={status} />
                  <strong>{count}</strong>
                </span>
              ))}
          </div>
        )}
      </div>

      {/* Recent applications */}
      <div className="spread" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0 }}>Recent applications</h2>
        <Link href="/underwriting">Open underwriting →</Link>
      </div>
      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th>App ID</th>
              <th>Applicant</th>
              <th className="num">Amount</th>
              <th>Status</th>
              <th>Received</th>
            </tr>
          </thead>
          <tbody>
            {recentApps.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty">
                  {loading ? "Loading…" : "No applications yet."}
                </td>
              </tr>
            ) : (
              recentApps.map((a) => (
                <tr key={String(a.id)}>
                  <td>
                    <Link href={`/underwriting/${a.id}`}>#{String(a.id)}</Link>
                  </td>
                  <td>{a.applicant_name}</td>
                  <td className="num">{usd(a.amount)}</td>
                  <td>
                    <StatusChip status={a.status} />
                  </td>
                  <td>{shortDate(a.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Recent loans */}
      <div className="spread" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0 }}>Recent loans</h2>
        <Link href="/servicing">Open servicing →</Link>
      </div>
      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th>Loan ID</th>
              <th>Borrower</th>
              <th className="num">Balance</th>
              <th className="num">Past due</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {recentLoans.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty">
                  {loading ? "Loading…" : "No loans yet."}
                </td>
              </tr>
            ) : (
              recentLoans.map((l) => (
                <tr key={String(l.id)}>
                  <td>
                    <Link href={`/servicing/${l.id}`}>#{String(l.id)}</Link>
                  </td>
                  <td>{l.applicant_name}</td>
                  <td className="num">{usd(l.balance)}</td>
                  <td className={`num${l.past_due > 0 ? " danger-text" : ""}`}>
                    {usd(l.past_due)}
                  </td>
                  <td>
                    <StatusChip status={l.status} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </main>
  );
}
