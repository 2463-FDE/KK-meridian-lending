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
  borrower?: string;
  principal: number;
  // Servicing exposes the CONTRACTUAL note rate under its accurate name.
  // The database column behind it is still `loans.apr` (legacy, tracked as
  // D19); the API is where that name stops. This is NOT the disclosed
  // federal APR -- that lives on the offer, and is the larger of the two
  // once a prepaid fee exists.
  note_rate_pct: number | null;
  note_rate_proven?: boolean;
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

export default function MyLoanPage() {
  return (
    <RequireRole allow={["borrower"]}>
      <MyLoanContent />
    </RequireRole>
  );
}

function MyLoanContent() {
  const [items, setItems] = useState<LoanRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Server-scoped to this borrower's own loans (gateway/app/main.py's
      // _borrower_loans) -- no client-side ownership filtering needed or
      // trusted here; if ownership can't be established the gateway 403s.
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
    load();
  }, [load]);

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
          <div className="grid grid-2" style={{ marginTop: 20 }}>
            {items.map((l) => (
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
                    <dt>Interest rate</dt>
                    {/* Null means the contractual rate was never recorded for
                        this loan -- it was boarded before the schedule was
                        stored, and `loans.apr` holds the disclosed APR for
                        those rows. Saying so is honest; printing that number
                        would state a rate the borrower was never quoted. */}
                    <dd>
                      {l.note_rate_pct != null
                        ? pct(l.note_rate_pct)
                        : "Not recorded for this loan"}
                    </dd>
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
