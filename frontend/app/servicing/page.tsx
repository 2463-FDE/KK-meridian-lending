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
  // Servicing exposes the CONTRACTUAL note rate under its accurate name, and
  // since the D19 contract step (db/migrations/0039) the database column is
  // called `note_rate_pct` too -- the API is no longer where a legacy name
  // stops. This is NOT the disclosed federal APR: that lives on the offer, and
  // is the larger of the two once a prepaid fee exists.
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

// Newest first by default. A loan boards with the highest id, so oldest-first
// put a freshly boarded loan on the last page -- and the search box used to
// filter only the rows already fetched, so typing its id on page 1 found
// nothing. Both halves are now the server's job.
const ORDER_OPTIONS = [
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
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
  const [order, setOrder] = useState("newest");
  // What is typed, and what has actually been searched for. Keeping them apart
  // is what stops the table changing under the operator as they type -- and it
  // is what lets the empty state name the id that was looked for rather than
  // whatever happens to be in the box now.
  const [loanIdInput, setLoanIdInput] = useState("");
  const [appliedLoanId, setAppliedLoanId] = useState("");
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
        order,
      });
      if (status) params.set("status", status);
      // Server-side: an id is looked up across the whole portfolio, not among
      // the 25 rows this page happens to hold.
      if (appliedLoanId) params.set("loan_id", appliedLoanId);
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
  }, [status, offset, order, appliedLoanId]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // No client-side filtering. The loan-id lookup is a server query so it reaches
  // the whole portfolio; filtering here would only ever search the rows already
  // fetched, which is the defect this replaced.
  const visible = items;

  const filtersActive = Boolean(status || appliedLoanId || order !== "newest");

  function runSearch() {
    setOffset(0);
    setAppliedLoanId(loanIdInput.trim());
  }

  function clearFilters() {
    setOffset(0);
    setStatus("");
    setOrder("newest");
    setLoanIdInput("");
    setAppliedLoanId("");
  }

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
          <label htmlFor="loan-id-search">Loan ID</label>
          <input
            id="loan-id-search"
            inputMode="numeric"
            value={loanIdInput}
            onChange={(e) => setLoanIdInput(e.target.value.replace(/[^0-9]/g, ""))}
            onKeyDown={(e) => {
              if (e.key === "Enter") runSearch();
            }}
            placeholder="e.g. 4471"
          />
        </div>
        <div className="field">
          <label htmlFor="loan-order">Sort</label>
          <select
            id="loan-order"
            value={order}
            onChange={(e) => {
              setOffset(0);
              setOrder(e.target.value);
            }}
          >
            {ORDER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <button className="btn-ghost" onClick={runSearch} disabled={loading}>
          Search
        </button>
        <button
          className="btn-ghost"
          onClick={clearFilters}
          disabled={loading || !filtersActive}
        >
          Clear
        </button>
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
              {/* "Rate" was ambiguous in the one place it matters most: the
                  column holds `note_rate_pct`, the CONTRACTUAL interest rate,
                  and the disclosed federal APR is a different and usually higher
                  number. A servicing screen that says only "Rate" invites a
                  staff member to quote it as the APR. */}
              <th className="num" title="Contractual interest rate; the federal APR is shown on the disclosure">
                Note rate
              </th>
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
                <td colSpan={9} className="empty" data-testid="servicing-empty">
                  {error ? (
                    "Unable to load loans."
                  ) : appliedLoanId ? (
                    <>
                      {/* Name the id that was searched for. "No loans match your
                          filters" left an operator unsure whether the loan is
                          absent or the filters are hiding it. */}
                      No serviced loan #{appliedLoanId} matches the current
                      filters.{" "}
                      <button className="btn-link" onClick={clearFilters}>
                        Clear filters
                      </button>
                    </>
                  ) : filtersActive ? (
                    <>
                      No loans match the current filters.{" "}
                      <button className="btn-link" onClick={clearFilters}>
                        Clear filters
                      </button>
                    </>
                  ) : (
                    "No loans are being serviced yet."
                  )}
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
