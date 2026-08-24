"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import RequireRole from "../../components/RequireRole";
import { apiGet, apiPost } from "../../lib/api";
import { shortDate } from "../../lib/format";

/**
 * The in-app reconciliation queue: the ONLY destination for a payment flagged
 * for human review.
 *
 * The client's decision of 2026-08-24 replaced D22's deferral with a review-only
 * contract, and named one destination -- "Meridian's internal in-app
 * reconciliation queue/dashboard" -- while ruling out email, Slack, PagerDuty,
 * webhooks and SMS before the freeze. There is no fallback channel behind this
 * page. If a flagged payment is not visible here, nobody is told at all, which
 * is why the queue is the first thing on the screen rather than a tab.
 *
 * **Two different things, kept visually apart, because the client asked for
 * exactly that.** They are not the same problem and they do not have the same
 * answer:
 *
 *   * a RECONCILIATION BREAK is the control's own finding -- our ledger and the
 *     processor's settlement file disagree about money. It is a fact about the
 *     books, produced by a job, and it is not about any one person's judgement.
 *   * a REVIEW CANDIDATE is a payment that RESEMBLES another one. It is not a
 *     duplicate, not a validity conclusion and not permission to move money --
 *     it is a question put to a human, and the human's answer is the only
 *     conclusion in the system.
 *
 * Merging them into one list would invite reading a candidate as a break: a
 * break says money is wrong, a candidate says please look. So they are separate
 * sections with separate headings, and the candidate section states what a flag
 * is not, in the words the server sends rather than a paraphrase this page
 * invented.
 *
 * **What this page cannot do.** There is no reverse, refund or adjust control
 * anywhere on it, deliberately. `confirmed_duplicate` is a classification; a
 * reversal is a money movement, and money movements go through the maker-checker
 * queue with the second person that requires (`/approvals`). Putting a reverse
 * button beside a disposition would make a flag one click from moving money,
 * which is the precise thing the client's wording forbids.
 */

interface QueuePayment {
  id: number;
  amount: string;
  method: string | null;
  captured_at: string | null;
  auth_status: string | null;
}

interface ReviewItem {
  id: number;
  created_at: string;
  signal_type: string;
  signal_category: "exact" | "heuristic";
  loan_id: number;
  correlation_ref: string | null;
  status: string;
  disposition: string | null;
  disposition_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  reviewed_by_role: string | null;
  payment: QueuePayment | null;
  related_payment: QueuePayment | null;
}

interface QueueResponse {
  items?: ReviewItem[];
  counts?: { open_exact: number; open_heuristic: number; reviewed: number };
  dispositions?: string[];
  note?: string;
}

interface Peek {
  ledger_total: number;
  settlement_total: number;
  last_successful_run: { id: number; at: string; loans_compared: number } | null;
  recent_failures: {
    id: number;
    at: string;
    outcome: string;
    breaks_found: number;
    break_value: string;
    error_code: string | null;
  }[];
}

/**
 * What each signal means, in a sentence a reviewer can act on.
 *
 * The server sends the signal name and its category; the prose is here because
 * it is presentation. What is NOT here is any suggestion of what to conclude --
 * "same reference seen twice" is an observation, "this is a duplicate" would be
 * the conclusion the client reserved for the human.
 */
const SIGNAL_PROSE: Record<string, string> = {
  exact_provider_transaction_id:
    "The processor returned a settlement reference another capture already holds. Elapsed time does not weaken this.",
  exact_idempotency_key:
    "The same idempotency key was presented again, so the client believed it was retrying one payment.",
  heuristic_30_minute_candidate:
    "Same loan, same amount, same payment source and same channel, inside 30 minutes. A second real installment can look exactly like this.",
};

const DISPOSITION_LABEL: Record<string, string> = {
  confirmed_duplicate: "Confirmed duplicate",
  legitimate_distinct_payment: "Legitimate distinct payment",
  requires_further_review: "Requires further review",
};

function amountLabel(payment: QueuePayment | null): string {
  // The server sends the amount as a STRING from NUMERIC(14,2). Rendering it
  // through `Number()` to reformat it would put a float back in the path the
  // column type was changed to keep floats out of (D12), so it is displayed as
  // sent, with the currency symbol added around it rather than into it.
  if (!payment) return "—";
  return `$${payment.amount}`;
}

function ReconciliationQueue() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [counts, setCounts] = useState<QueueResponse["counts"] | null>(null);
  const [serverNote, setServerNote] = useState<string | null>(null);
  const [peek, setPeek] = useState<Peek | null>(null);
  const [showReviewed, setShowReviewed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const status = showReviewed ? "reviewed" : "open";
      const [queue, breaks] = await Promise.all([
        apiGet(`/lss/reconciliation/review-queue?status=${status}`) as Promise<QueueResponse>,
        apiGet("/lss/reconciliation/peek") as Promise<Peek>,
      ]);
      setItems(queue.items ?? []);
      setCounts(queue.counts ?? null);
      // The server's sentence about what a flag is not, displayed rather than
      // rewritten here. A paraphrase in the browser is a second copy of a
      // client instruction, free to soften as the page is edited.
      setServerNote(queue.note ?? null);
      setPeek(breaks);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The reconciliation queue could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [showReviewed]);

  useEffect(() => {
    load();
  }, [load]);

  async function disposition(itemId: number, choice: string) {
    setBusyId(itemId);
    setNotice(null);
    setError(null);
    try {
      await apiPost(`/lss/reconciliation/review-queue/${itemId}/disposition`, {
        disposition: choice,
        note: notes[itemId] || null,
      });
      setNotice(
        `Review item ${itemId} recorded as “${DISPOSITION_LABEL[choice] ?? choice}”. ` +
          `No money moved — a reversal goes through Approvals.`
      );
      await load();
    } catch (e) {
      // The server's own words. A 409 here means someone else answered first,
      // and that is worth reading exactly rather than as "something failed".
      setError(e instanceof Error ? e.message : "The disposition could not be recorded.");
    } finally {
      setBusyId(null);
    }
  }

  const brokenRuns = (peek?.recent_failures ?? []).filter((r) => r.breaks_found > 0);

  return (
    <div className="wrap">
      <h1>Reconciliation</h1>

      {notice ? <p className="alert alert-success">{notice}</p> : null}
      {error ? <p className="alert alert-error">{error}</p> : null}

      {/* --- payments flagged for a human to look at ------------------------ */}
      <section>
        <h2>Payment review candidates</h2>
        <p className="sub">
          {serverNote ??
            "Candidates for human reconciliation review. A flag is not a duplicate finding, not a validity conclusion, and not permission to move money."}
        </p>

        {counts ? (
          <p className="muted">
            {counts.open_exact} exact-match {counts.open_exact === 1 ? "signal" : "signals"} ·{" "}
            {counts.open_heuristic} within-30-minutes{" "}
            {counts.open_heuristic === 1 ? "candidate" : "candidates"} open ·{" "}
            {counts.reviewed} already reviewed
          </p>
        ) : null}

        <p className="row">
          <button
            className="btn-ghost btn-sm"
            onClick={() => setShowReviewed((v) => !v)}
          >
            {showReviewed ? "Show open items" : "Show reviewed items"}
          </button>
        </p>

        {loading ? (
          <div className="card empty">Loading…</div>
        ) : items.length === 0 ? (
          <div className="card empty">
            {showReviewed
              ? "Nothing has been reviewed yet."
              : "No payments are waiting for review."}
          </div>
        ) : (
          items.map((item) => (
            <section className="card" key={item.id}>
              <div className="card-head">
                <span className="card-title">
                  Potential duplicate — review required
                </span>
                <span className="muted">
                  {item.signal_category === "exact"
                    ? "Exact match signal"
                    : "Within 30 minutes"}
                </span>
              </div>

              <p>{SIGNAL_PROSE[item.signal_type] ?? item.signal_type}</p>

              {/* `table-wrap` is the scroll container every other table on
                  the site uses; a bare <table> overflows the card on a narrow
                  screen instead of scrolling inside it. */}
              <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Payment</th>
                    <th className="num">Amount</th>
                    <th>Channel</th>
                    <th>Captured</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Flagged · {item.payment?.id ?? "—"}</td>
                    <td className="num">{amountLabel(item.payment)}</td>
                    <td>{item.payment?.method ?? "—"}</td>
                    <td>
                      {item.payment?.captured_at
                        ? shortDate(item.payment.captured_at)
                        : "not captured"}
                    </td>
                    <td>{item.payment?.auth_status ?? "—"}</td>
                  </tr>
                  <tr>
                    <td>
                      {item.related_payment
                        ? `Resembles · ${item.related_payment.id}`
                        : "Resembles · not identified"}
                    </td>
                    <td className="num">{amountLabel(item.related_payment)}</td>
                    <td>{item.related_payment?.method ?? "—"}</td>
                    <td>
                      {item.related_payment?.captured_at
                        ? shortDate(item.related_payment.captured_at)
                        : "—"}
                    </td>
                    <td>{item.related_payment?.auth_status ?? "—"}</td>
                  </tr>
                </tbody>
              </table>
              </div>

              <p className="muted">
                Flagged {shortDate(item.created_at)} ·{" "}
                <Link href={`/servicing/${item.loan_id}`}>Loan {item.loan_id}</Link>
                {item.correlation_ref ? ` · trace ${item.correlation_ref}` : ""}
              </p>

              {item.status === "reviewed" ? (
                <p className="alert alert-success">
                  {DISPOSITION_LABEL[item.disposition ?? ""] ?? item.disposition} —
                  recorded by {item.reviewed_by_role} {item.reviewed_by}
                  {item.reviewed_at ? ` on ${shortDate(item.reviewed_at)}` : ""}.
                  {item.disposition_note ? ` “${item.disposition_note}”` : ""} This
                  answer cannot be changed.
                </p>
              ) : (
                <>
                  <label htmlFor={`note-${item.id}`}>
                    What you found (optional, stored with your answer)
                  </label>
                  <input
                    id={`note-${item.id}`}
                    className="inp"
                    value={notes[item.id] ?? ""}
                    onChange={(e) =>
                      setNotes((n) => ({ ...n, [item.id]: e.target.value }))
                    }
                  />
                  <div className="row">
                    {/* The three the client authorised, in the order a reviewer
                        reaches for them. A fourth would be a policy nobody
                        approved, and the server refuses one. */}
                    {["confirmed_duplicate", "legitimate_distinct_payment",
                      "requires_further_review"].map((choice) => (
                      <button
                        key={choice}
                        className="btn btn-sm"
                        disabled={busyId === item.id}
                        onClick={() => disposition(item.id, choice)}
                      >
                        {DISPOSITION_LABEL[choice]}
                      </button>
                    ))}
                  </div>
                  <p className="muted">
                    Recording an answer moves no money. A reversal is a separate,
                    two-person decision in <Link href="/approvals">Approvals</Link>.
                  </p>
                </>
              )}
            </section>
          ))
        )}
      </section>

      {/* --- the control's own findings, kept apart -------------------------- */}
      <section>
        <h2>Reconciliation breaks</h2>
        <p className="sub">
          The ledger compared against the processor&rsquo;s settlement file. These are
          the control&rsquo;s own findings about money, not questions put to a
          reviewer — a different thing from the candidates above, and answered by
          a different kind of work.
        </p>

        {loading ? (
          <div className="card empty">Loading…</div>
        ) : !peek ? (
          <div className="card empty">The comparison could not be read.</div>
        ) : (
          <section className="card">
            <div className="spread">
              <span>Ledger total</span>
              <span className="num">${peek.ledger_total}</span>
            </div>
            <div className="spread">
              <span>Settlement total</span>
              <span className="num">${peek.settlement_total}</span>
            </div>
            <p className="muted">
              {/* Two equal numbers are not a reconciliation. D7: the totals alone
                  could not distinguish "these agree" from "nothing has checked
                  since March", so the run is stated beside them. */}
              {peek.last_successful_run
                ? `Last compared ${shortDate(peek.last_successful_run.at)} across ${
                    peek.last_successful_run.loans_compared
                  } loans.`
                : "The comparison has never completed, so the two totals above prove nothing."}
            </p>

            {brokenRuns.length === 0 ? (
              <p className="muted">No breaks recorded in the recent runs.</p>
            ) : (
              <ul>
                {brokenRuns.map((r) => (
                  <li key={r.id}>
                    {shortDate(r.at)} — {r.breaks_found}{" "}
                    {r.breaks_found === 1 ? "break" : "breaks"}, value{" "}
                    {r.break_value}
                    {r.error_code ? ` (${r.error_code})` : ""}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </section>
    </div>
  );
}

export default function ReconciliationPage() {
  // Staff only at the browser; the gateway enforces the same rule server-side,
  // and servicing verifies the signed principal behind it a third time.
  return (
    <RequireRole allow={["csr", "underwriter", "admin"]}>
      <ReconciliationQueue />
    </RequireRole>
  );
}
