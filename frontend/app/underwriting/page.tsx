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

interface AppsResponse {
  items: AppRow[];
  total: number;
  limit: number;
  offset: number;
}

const PAGE_SIZE = 25;
// Bug fix: these values used to be "pending"/"in_review"/"approved"/"denied",
// but the real applications.status column only ever held 'submitted' or
// 'funded' -- the decision outcome was never written back onto it (see
// origination-service/app/routers/applications.py::run_decision). Matches
// the actual status values now.
const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "submitted", label: "Pending decision" },
  { value: "in_review", label: "In review" },
  { value: "approved", label: "Approved" },
  { value: "denied", label: "Denied" },
  { value: "funded", label: "Funded" },
];

// Newest first by DEFAULT, because a just-submitted application is the one an
// underwriter is looking for. Ordering is on the application id server-side --
// see `routers/applications.py` for why not `created_at`.
const ORDER_OPTIONS = [
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
];

function prettyPurpose(p: string): string {
  return (p || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function UnderwritingPage() {
  return (
    <RequireRole allow={["underwriter", "admin", "csr"]}>
      <UnderwritingContent />
    </RequireRole>
  );
}

function UnderwritingContent() {
  const [status, setStatus] = useState("");
  const [order, setOrder] = useState("newest");
  // What is typed, and what has actually been searched for. Keeping them apart
  // stops the table changing under the underwriter as they type, and lets the
  // empty state name the id that was looked for rather than whatever is in the
  // box now. Same split the servicing portfolio uses.
  const [appIdInput, setAppIdInput] = useState("");
  const [appliedAppId, setAppliedAppId] = useState("");
  const [offset, setOffset] = useState(0);

  const [data, setData] = useState<AppsResponse | null>(null);
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
      // Server-side: an id is looked up across the WHOLE pipeline, not among
      // the 25 rows this page happens to hold. That was the defect -- an
      // application outside the current page could not be found by typing its
      // id, on the screen an underwriter starts their day on.
      if (appliedAppId) params.set("app_id", appliedAppId);
      const res = (await apiGet(
        `/los/applications?${params.toString()}`
      )) as AppsResponse;
      setData(res);
    } catch (err) {
      const msg =
        err && typeof err === "object" && "detail" in err
          ? String((err as { detail: unknown }).detail)
          : err instanceof Error
            ? err.message
            : "Could not load applications.";
      setError(msg);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [status, offset, order, appliedAppId]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // No client-side filtering. The application-id lookup is a server query so it
  // reaches the whole pipeline; filtering here would only ever search the rows
  // already fetched, which is exactly the defect this replaced.
  //
  // The applicant-name half of the old box is gone rather than kept alongside
  // it. It searched 25 rows and looked like it searched the pipeline, so a
  // name that was simply on page three read as "no such applicant" -- a more
  // convincing wrong answer than no search at all.
  const visible = items;

  const filtersActive = Boolean(status || appliedAppId || order !== "newest");

  function runSearch() {
    setOffset(0);
    setAppliedAppId(appIdInput.trim());
  }

  function clearFilters() {
    setOffset(0);
    setStatus("");
    setOrder("newest");
    setAppIdInput("");
    setAppliedAppId("");
  }

  // KPI summary derived from the current page of applications.
  const pendingCount = items.filter((a) =>
    ["submitted", "in_review"].includes((a.status || "").toLowerCase())
  ).length;
  const approvedCount = items.filter(
    (a) => (a.status || "").toLowerCase() === "approved"
  ).length;
  const requestedTotal = items.reduce((sum, a) => sum + (a.amount || 0), 0);

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <main className="wrap">
      <div className="spread">
        <div>
          <h1>Underwriting console</h1>
          <p className="sub" style={{ margin: 0 }}>
            Application pipeline — review, decision, and board new loans.
          </p>
        </div>
      </div>

      {/* KPI summary cards */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Awaiting decision (page)</div>
          <div className="kpi-value">{pendingCount}</div>
          <div className="kpi-sub">{total} applications total</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Approved (page)</div>
          <div className="kpi-value">{approvedCount}</div>
          <div className="kpi-sub">Ready to board</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Requested (page)</div>
          <div className="kpi-value">{usd(requestedTotal)}</div>
          <div className="kpi-sub">Sum of amounts on this page</div>
        </div>
      </div>

      {/* Filters */}
      <div className="toolbar">
        <div className="field">
          {/* `htmlFor`/`id`, so the label is actually associated with the
              control. Without it a screen reader announces an unlabelled
              select, and `getByLabel("Status")` resolves nothing -- which is
              how the gap was noticed: a test meant to prove the status filter
              composes with the id lookup silently SKIPPED instead of running. */}
          <label htmlFor="app-status">Status</label>
          <select
            id="app-status"
            value={status}
            onChange={(e) => {
              // Reset to page one: an offset that survives a filter change
              // points at a row number in a list that no longer exists.
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
          <label htmlFor="app-id-search">Application ID</label>
          {/* Digits only, and an exact id. This is a server lookup across the
              whole pipeline, not a substring match over the current page --
              which is why the label names the ID rather than promising to
              search applicants too. */}
          <input
            id="app-id-search"
            inputMode="numeric"
            value={appIdInput}
            onChange={(e) => setAppIdInput(e.target.value.replace(/[^0-9]/g, ""))}
            onKeyDown={(e) => {
              if (e.key === "Enter") runSearch();
            }}
            placeholder="e.g. 412"
          />
        </div>
        <div className="field">
          <label htmlFor="app-order">Sort</label>
          <select
            id="app-order"
            value={order}
            onChange={(e) => {
              // Reset to page one: an offset that survives a sort change points
              // at a row number in a list that no longer exists.
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
              <th>App ID</th>
              <th>Applicant</th>
              <th className="num">Amount</th>
              <th className="num">Term</th>
              <th>Purpose</th>
              <th>Status</th>
              <th>Received</th>
            </tr>
          </thead>
          <tbody>
            {loading && !data ? (
              <tr>
                <td colSpan={7} className="empty">
                  Loading applications…
                </td>
              </tr>
            ) : visible.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty">
                  {error ? (
                    "Unable to load applications."
                  ) : appliedAppId ? (
                    <>
                      {/* Name the id that was searched for. "No applications
                          match your filters" left an underwriter unsure whether
                          the application is absent or the filters are hiding
                          it -- and with a client-side search it was usually the
                          second, which is what made the old box misleading
                          rather than merely limited. */}
                      No application #{appliedAppId} matches the current filters.{" "}
                      <button className="btn-link" onClick={clearFilters}>
                        Clear filters
                      </button>
                    </>
                  ) : filtersActive ? (
                    <>
                      No applications match the current filters.{" "}
                      <button className="btn-link" onClick={clearFilters}>
                        Clear filters
                      </button>
                    </>
                  ) : (
                    "No applications have been submitted yet."
                  )}
                </td>
              </tr>
            ) : (
              visible.map((a) => (
                <tr key={String(a.id)}>
                  <td>
                    <Link href={`/underwriting/${a.id}`}>#{String(a.id)}</Link>
                  </td>
                  <td>{a.applicant_name}</td>
                  <td className="num">{usd(a.amount)}</td>
                  <td className="num">{a.term_months} mo</td>
                  <td>{prettyPurpose(a.purpose)}</td>
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

      <div className="pager">
        <span>
          Page {page} of {pageCount} · {total} applications
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
