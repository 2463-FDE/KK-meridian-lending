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

/**
 * Adverse-action reason monitoring, as `reason_distribution.py` computes it.
 *
 * Every figure here is the server's. Nothing is recomputed in the browser --
 * a second opinion about a regulatory count is a second answer, and the whole
 * value of this panel is that it reports what the decision record actually
 * holds.
 */
interface ReasonVersion {
  model_version: string;
  decisions: number;
  distinct_reasons: number;
  /** Denials recorded with NO reason at all. Spec 0003 says this should be 0. */
  missing_reason: number;
  /** reason code -> count, already ordered by the server. */
  reason_frequency: Record<string, number>;
}

interface ReasonDistribution {
  window: { since: string | null; until: string | null };
  outcomes_counted: string[];
  versions: ReasonVersion[];
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
  // Its own request, its own loading flag, its own error. The portfolio load
  // above already puts two calls under one `catch`; adding a third to it would
  // mean a governance panel failing to load blanks the applications and loans
  // beside it. `/reconciliation` was split for exactly this reason (PR #81).
  const [reasons, setReasons] = useState<ReasonDistribution | null>(null);
  const [reasonsLoading, setReasonsLoading] = useState(true);
  const [reasonsError, setReasonsError] = useState<string | null>(null);

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

  const loadReasons = useCallback(async () => {
    setReasonsLoading(true);
    setReasonsError(null);
    try {
      setReasons(
        (await apiGet(
          "/los/applications/fair-lending/reason-distribution",
        )) as ReasonDistribution,
      );
    } catch (err) {
      setReasons(null);
      setReasonsError(
        errMsg(err, "Adverse-action reason monitoring could not be loaded."),
      );
    } finally {
      setReasonsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadReasons();
  }, [loadReasons]);

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

      {/* Adverse-action reason monitoring (spec 0003 §1.3).

          Named for what it is. It reports which adverse-action reasons the
          model actually emitted, per model version, over a stated window. It is
          NOT a protected-class disparity analysis: the client prohibited runtime
          protected-class data and inferred proxies (ZIP/ZIP3 among them), and
          the runtime ZIP screen was retired on that instruction. Nothing here
          reads, infers or renders a protected characteristic.

          No threshold, no verdict, no pass/fail. `reason_distribution.py`
          deliberately sets none -- "too few distinct reasons" is a compliance
          judgement this repository has no authority to make -- and a panel that
          added one in the browser would be inventing exactly that authority. */}
      <div className="spread" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0 }} data-testid="reason-monitoring-heading">
          Adverse-action reason monitoring
        </h2>
        <button
          className="btn-ghost"
          onClick={loadReasons}
          disabled={reasonsLoading}
        >
          {reasonsLoading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <p className="sub" style={{ marginTop: 6 }} data-testid="reason-monitoring-qualifier">
        This monitors adverse-action reason distribution. It is not a
        protected-class disparity analysis or a production fairness
        determination.
      </p>

      {reasonsError ? (
        <div className="alert alert-error" data-testid="reason-monitoring-error">
          {reasonsError}
        </div>
      ) : null}

      {reasonsLoading && !reasons ? (
        <div className="card empty">Loading…</div>
      ) : !reasons ? (
        !reasonsError ? (
          <div className="card empty">
            Reason monitoring is unavailable. That is a gap in this panel, not a
            statement about the decisions themselves.
          </div>
        ) : null
      ) : (
        <>
          <p className="hint" style={{ marginTop: 0 }} data-testid="reason-monitoring-window">
            {/* The window is stated even when it is unbounded: "all time" is a
                window worth saying out loud, and a report that does not state
                its own cannot be compared with another one. */}
            Reporting window:{" "}
            {reasons.window.since ?? "all time"}
            {" → "}
            {reasons.window.until ?? "now"}
            {reasons.outcomes_counted?.length
              ? ` · outcomes counted: ${reasons.outcomes_counted.join(", ")}`
              : ""}
          </p>

          {reasons.versions.length === 0 ? (
            <div className="card empty" data-testid="reason-monitoring-empty">
              No decisions carrying an adverse-action outcome were recorded in
              this window.
            </div>
          ) : (
            reasons.versions.map((v) => (
              <div
                className="card"
                key={v.model_version}
                data-testid={`reason-version-${v.model_version}`}
                style={{ marginTop: 12 }}
              >
                <div className="spread">
                  <div className="card-title" style={{ margin: 0 }}>
                    Model version <strong>{v.model_version}</strong>
                  </div>
                  <span className="muted">
                    {v.decisions} adverse{" "}
                    {v.decisions === 1 ? "decision" : "decisions"}
                  </span>
                </div>

                <div className="row" style={{ gap: 24, margin: "10px 0" }}>
                  <span>
                    Distinct reasons{" "}
                    <strong data-testid={`reason-distinct-${v.model_version}`}>
                      {v.distinct_reasons}
                    </strong>
                  </span>
                  <span>
                    {/* Spec 0003 says this should be zero: a denial with no
                        reason on record is the Reg B defect itself, so it is
                        shown beside the distribution rather than buried. */}
                    No-reason decisions{" "}
                    <strong
                      className={v.missing_reason > 0 ? "danger-text" : undefined}
                      data-testid={`reason-missing-${v.model_version}`}
                    >
                      {v.missing_reason}
                    </strong>
                  </span>
                </div>

                {Object.keys(v.reason_frequency).length === 0 ? (
                  <p className="muted" style={{ margin: 0 }}>
                    No reasons were recorded for this model version.
                  </p>
                ) : (
                  <div className="table-wrap">
                    <table data-testid={`reason-table-${v.model_version}`}>
                      <thead>
                        <tr>
                          <th>Reason</th>
                          <th className="num">Count</th>
                        </tr>
                      </thead>
                      <tbody>
                        {/* Server order preserved -- it is already sorted by
                            frequency then code, and re-sorting here would be a
                            second opinion about the same numbers. */}
                        {Object.entries(v.reason_frequency).map(
                          ([reason, count]) => (
                            <tr key={reason}>
                              <td>{reason}</td>
                              <td className="num">{count}</td>
                            </tr>
                          ),
                        )}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))
          )}
        </>
      )}

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
