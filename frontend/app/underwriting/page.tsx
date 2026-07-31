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

function prettyPurpose(p: string): string {
  return (p || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function UnderwritingPage() {
  return (
    <RequireRole allow={["underwriter", "admin"]}>
      <UnderwritingContent />
    </RequireRole>
  );
}

function UnderwritingContent() {
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
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
      });
      if (status) params.set("status", status);
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
  }, [status, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // Client-side applicant/id search filter on top of the server page.
  const visible = search.trim()
    ? items.filter(
        (a) =>
          String(a.id).toLowerCase().includes(search.trim().toLowerCase()) ||
          (a.applicant_name || "")
            .toLowerCase()
            .includes(search.trim().toLowerCase())
      )
    : items;

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
          <label>Search applicant / ID</label>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="e.g. Maria or 4471"
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
                  {error
                    ? "Unable to load applications."
                    : "No applications match your filters."}
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
