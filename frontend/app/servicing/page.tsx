"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import RequireRole from "../../components/RequireRole";
import StatusChip from "../../components/StatusChip";
import { apiGet } from "../../lib/api";
import { usd, pct, shortDate } from "../../lib/format";

interface LoanRow {
  id: string | number;
  applicant_name: string;
  principal: number;
  // Servicing exposes the CONTRACTUAL note rate under its accurate name.
  // The database column behind it is still `loans.apr` (legacy, tracked as
  // D19); the API is where that name stops. This is NOT the disclosed
  // federal APR -- that lives on the offer, and is the larger of the two
  // once a prepaid fee exists.
  note_rate_pct: number;
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

const PAGE_SIZE = 25;
const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "current", label: "Current" },
  { value: "delinquent", label: "Delinquent" },
  { value: "paid_off", label: "Paid off" },
];

export default function ServicingPage() {
  return (
    <RequireRole allow={["csr", "underwriter", "admin"]}>
      <ServicingContent />
    </RequireRole>
  );
}

function ServicingContent() {
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const [data, setData] = useState<LoansResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (status) params.set("status", status);
      const res = (await apiGet(`/lss/loans?${params.toString()}`)) as LoansResponse;
      setData(res);
    } catch (err) {
      const msg =
        err && typeof err === "object" && "detail" in err
          ? String((err as { detail: unknown }).detail)
          : err instanceof Error
            ? err.message
            : "Could not load loans.";
      setError(msg);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [status, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // Client-side loan-id search filter on top of the server page.
  const visible = search.trim()
    ? items.filter((l) =>
        String(l.id).toLowerCase().includes(search.trim().toLowerCase())
      )
    : items;

  // KPI summary derived from the current page of loans.
  const portfolioBalance = items.reduce((sum, l) => sum + (l.balance || 0), 0);
  const activeCount = items.filter(
    (l) => (l.status || "").toLowerCase() !== "paid_off"
  ).length;
  const delinquentCount = items.filter((l) =>
    ["delinquent", "past_due"].includes((l.status || "").toLowerCase())
  ).length;

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <main className="wrap">
      <div className="spread">
        <div>
          <h1>Servicing dashboard</h1>
          <p className="sub" style={{ margin: 0 }}>
            Loan portfolio overview — balances, delinquency, and accounts.
          </p>
        </div>
      </div>

      {/* KPI summary cards */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Portfolio balance (page)</div>
          <div className="kpi-value">{usd(portfolioBalance)}</div>
          <div className="kpi-sub">Sum of balances on this page</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Active loans (page)</div>
          <div className="kpi-value">{activeCount}</div>
          <div className="kpi-sub">{total} loans total</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Delinquent (page)</div>
          <div className={`kpi-value${delinquentCount > 0 ? " danger" : ""}`}>
            {delinquentCount}
          </div>
          <div className="kpi-sub">Past-due accounts</div>
        </div>
      </div>

      {/* Filters */}
      <div className="toolbar">
        <div className="field">
          <label>Status</label>
          <select
            value={status}
            onChange={(e) => {
              setOffset(0);
              setStatus(e.target.value);
            }}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Search loan ID</label>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="e.g. 4471"
          />
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error ? <div className="alert alert-error">{error}</div> : null}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Loan ID</th>
              <th>Borrower</th>
              <th className="num">Principal</th>
              <th className="num">Rate</th>
              <th className="num">Term</th>
              <th>Status</th>
              <th className="num">Balance</th>
              <th className="num">Past due</th>
              <th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {loading && !data ? (
              <tr>
                <td colSpan={9} className="empty">
                  Loading loans…
                </td>
              </tr>
            ) : visible.length === 0 ? (
              <tr>
                <td colSpan={9} className="empty">
                  {error
                    ? "Unable to load loans."
                    : "No loans match your filters."}
                </td>
              </tr>
            ) : (
              visible.map((l) => (
                <tr key={String(l.id)}>
                  <td>
                    <Link href={`/servicing/${l.id}`}>#{String(l.id)}</Link>
                  </td>
                  <td>{l.applicant_name}</td>
                  <td className="num">{usd(l.principal)}</td>
                  <td className="num">{pct(l.note_rate_pct)}</td>
                  <td className="num">{l.term_months} mo</td>
                  <td>
                    <StatusChip status={l.status} />
                  </td>
                  <td className="num">{usd(l.balance)}</td>
                  <td className={`num${l.past_due > 0 ? " danger-text" : ""}`}>
                    {usd(l.past_due)}
                  </td>
                  <td>{shortDate(l.opened_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <span>
          Page {page} of {pageCount} · {total} loans
        </span>
        <div className="row">
          <button
            className="btn-ghost btn-sm"
            disabled={offset === 0 || loading}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Prev
          </button>
          <button
            className="btn-ghost btn-sm"
            disabled={offset + PAGE_SIZE >= total || loading}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next →
          </button>
        </div>
      </div>
    </main>
  );
}
