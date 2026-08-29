"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import RequireRole from "../../components/RequireRole";
import { apiGet, apiPost } from "../../lib/api";
import { shortDate } from "../../lib/format";
import { comparisonStatement } from "../../lib/reconciliation";

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

/**
 * One transaction that did not tie out, as the run recorded it.
 *
 * Amounts are strings because they are strings in the ledger and in the
 * settlement file. Parsing them into numbers here would introduce a second
 * representation of a figure whose exactness is the entire point of the
 * comparison.
 */
interface TransactionBreak {
  kind: string;
  loan_id: number;
  processor_ref: string | null;
  ledger: string;
  settlement: string;
  difference: string;
}

/** The last run's own evidence. Every field is read back, never recomputed. */
interface RunEvidence {
  id: number;
  outcome: string;
  started_at: string | null;
  finished_at: string | null;
  window_start: string | null;
  window_end: string | null;
  source: Record<string, unknown> | null;
  loans_compared: number;
  references_compared: number;
  unreferenced_captures: number;
  out_of_scope_captures: number;
  breaks_found: number;
  break_value: string;
  threshold_value: string;
  error_code: string | null;
  breaks: TransactionBreak[];
  /** How many of `breaks_found` are actually in `breaks`. */
  breaks_recorded: number;
  /** True when the run found more breaks than it stored. */
  breaks_truncated: boolean;
  max_recorded_breaks: number;
}

interface LatestRun {
  run: RunEvidence | null;
  note: string;
}

/**
 * What the run read, named in a way an operator can act on.
 *
 * `source` is JSONB the job wrote, so its shape is the job's rather than this
 * page's. A known `file` key is shown as a filename; anything else is shown as
 * its JSON rather than dropped, because a source this page cannot label is
 * still evidence and hiding it would make the run look less specified than it
 * was. An absent source says so plainly.
 */
function sourceLabel(source: Record<string, unknown> | null): string {
  if (!source || Object.keys(source).length === 0) return "not recorded";
  const file = source.file;
  if (typeof file === "string" && file) return file;
  return JSON.stringify(source);
}

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
  // Two independent loads, and deliberately two of everything that describes
  // them. Review of PR #81: both requests were awaited in one `Promise.all`
  // under one `catch`, so a failure of `/peek` -- the break SUMMARY, which
  // decides nothing about a flagged payment -- threw before `setItems` ran and
  // left the candidate list empty. The one destination the client authorised for
  // a flagged payment went blank because an unrelated request failed, and
  // "nothing to review" and "the fetch broke" looked identical on screen.
  //
  // So the queue renders whenever ITS OWN call succeeded, and each section owns
  // its loading and error state. The sections are two different findings (that
  // is the whole layout); they are now two different failures too.
  const [queueLoading, setQueueLoading] = useState(true);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [latest, setLatest] = useState<LatestRun | null>(null);
  // A third section, so a third pair of loading/error flags. Same reasoning as
  // the split above: the run evidence failing to load says nothing about the
  // candidates or the totals, and must not blank either.
  const [latestLoading, setLatestLoading] = useState(true);
  const [latestError, setLatestError] = useState<string | null>(null);
  const [peekLoading, setPeekLoading] = useState(true);
  const [peekError, setPeekError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});

  const loadQueue = useCallback(async () => {
    setQueueLoading(true);
    setQueueError(null);
    try {
      const status = showReviewed ? "reviewed" : "open";
      const queue = (await apiGet(
        `/lss/reconciliation/review-queue?status=${status}`,
      )) as QueueResponse;
      setItems(queue.items ?? []);
      setCounts(queue.counts ?? null);
      // The server's sentence about what a flag is not, displayed rather than
      // rewritten here. A paraphrase in the browser is a second copy of a
      // client instruction, free to soften as the page is edited.
      setServerNote(queue.note ?? null);
    } catch (e) {
      setQueueError(
        e instanceof Error ? e.message : "The review queue could not be loaded.",
      );
    } finally {
      setQueueLoading(false);
    }
  }, [showReviewed]);

  const loadPeek = useCallback(async () => {
    setPeekLoading(true);
    setPeekError(null);
    try {
      setPeek((await apiGet("/lss/reconciliation/peek")) as Peek);
    } catch (e) {
      setPeekError(
        e instanceof Error ? e.message : "The break comparison could not be read.",
      );
    } finally {
      setPeekLoading(false);
    }
  }, []);

  const loadLatest = useCallback(async () => {
    setLatestLoading(true);
    setLatestError(null);
    try {
      setLatest((await apiGet("/lss/reconciliation/latest")) as LatestRun);
    } catch (e) {
      setLatestError(
        e instanceof Error ? e.message : "The last run could not be read.",
      );
    } finally {
      setLatestLoading(false);
    }
  }, []);

  useEffect(() => {
    loadLatest();
  }, [loadLatest]);

  useEffect(() => {
    loadQueue();
  }, [loadQueue]);

  useEffect(() => {
    loadPeek();
  }, [loadPeek]);

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
      // Only the queue is reloaded: recording a disposition cannot change the
      // ledger-versus-settlement comparison, so re-reading the break summary
      // would be a request that can only fail.
      await loadQueue();
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

        {/* The queue's OWN error, and it must not read as an empty queue.
            "No payments are waiting for review" is a claim; a failed fetch
            cannot support it. */}
        {queueError ? <p className="alert alert-error">{queueError}</p> : null}

        {queueLoading ? (
          <div className="card empty">Loading…</div>
        ) : queueError ? (
          <div className="card empty">
            The review queue could not be read, so this list is not a statement
            about whether anything is waiting.
          </div>
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

        {/* Scoped to this section. A break summary that cannot be read says
            nothing about the candidates above it, and used to blank them. */}
        {peekError ? <p className="alert alert-error">{peekError}</p> : null}

        {peekLoading ? (
          <div className="card empty">Loading…</div>
        ) : !peek ? (
          <div className="card empty">
            The comparison could not be read. That is a gap in this panel, not a
            statement that the books agree.
          </div>
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
            <p className="muted" data-testid="comparison-statement">
              {/* Two equal numbers are not a reconciliation. D7: the totals alone
                  could not distinguish "these agree" from "nothing has checked
                  since March", so the run is stated beside them.

                  The sentence lives in lib/reconciliation.ts because it has three
                  branches, not two: `last_successful_run` is null both when
                  nothing has ever run AND when runs executed and found breaks.
                  This line used to call the second case "never completed", which
                  described a working control as one that had never run. */}
              {comparisonStatement(peek, shortDate)}
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

        {/* The last run's own evidence.

            Everything here was written by the job and read back unchanged --
            no figure on this panel is computed in the browser, and opening the
            page starts nothing. A control that ran because somebody looked at
            it would not be a scheduled control, and there is deliberately no
            "run now" button anywhere on this screen: the scheduler owns when
            reconciliation happens.

            Counts are shown even when they are zero, because zero is an
            answer. "Unreferenced captures: 0" says the run could match every
            capture it saw; a blank says nothing and reads as reassurance. */}
        <h3 className="section-title" data-testid="recon-latest-heading">
          Latest run
        </h3>

        {latestError ? (
          <p className="alert alert-error">{latestError}</p>
        ) : null}

        {latestLoading ? (
          <div className="card empty">Loading…</div>
        ) : !latest ? (
          <div className="card empty">
            The last run could not be read. That is a gap in this panel, not a
            statement that the books agree.
          </div>
        ) : !latest.run ? (
          /* Never run is not a clean result, and must not render as an empty
             break table under a heading that implies one (D7). */
          <div className="card empty" data-testid="recon-never-run">
            {latest.note}
          </div>
        ) : (
          <>
            <section className="card" data-testid="recon-latest-run">
              <div className="spread">
                <span>Outcome</span>
                <span data-testid="recon-outcome">{latest.run.outcome}</span>
              </div>
              <div className="spread">
                <span>Started</span>
                <span>{shortDate(latest.run.started_at ?? "")}</span>
              </div>
              <div className="spread">
                <span>Finished</span>
                <span>
                  {latest.run.finished_at
                    ? shortDate(latest.run.finished_at)
                    : "did not finish"}
                </span>
              </div>
              <div className="spread">
                <span>Window</span>
                <span>
                  {latest.run.window_start && latest.run.window_end
                    ? `${latest.run.window_start} → ${latest.run.window_end}`
                    : "not recorded"}
                </span>
              </div>
              <div className="spread">
                <span>Source</span>
                <span>{sourceLabel(latest.run.source)}</span>
              </div>
              <div className="spread">
                <span>Loans compared</span>
                <span className="num">{latest.run.loans_compared}</span>
              </div>
              <div className="spread">
                {/* How FINE the comparison was. Many loans and few references
                    means coarse per-loan totals were compared, which is the
                    state this control was fixed out of. */}
                <span>References compared</span>
                <span className="num">{latest.run.references_compared}</span>
              </div>
              <div className="spread">
                <span>Unreferenced captures</span>
                <span className="num">{latest.run.unreferenced_captures}</span>
              </div>
              <div className="spread">
                <span>Out-of-scope captures</span>
                <span className="num">{latest.run.out_of_scope_captures}</span>
              </div>
              <div className="spread">
                <span>Breaks found</span>
                <span className="num" data-testid="recon-breaks-found">
                  {latest.run.breaks_found}
                </span>
              </div>
              <div className="spread">
                <span>Break value</span>
                <span className="num">${latest.run.break_value}</span>
              </div>
              <div className="spread">
                <span>Threshold</span>
                <span className="num">${latest.run.threshold_value}</span>
              </div>
              {latest.run.error_code ? (
                <div className="spread">
                  <span>Error code</span>
                  <span data-testid="recon-error-code">
                    {latest.run.error_code}
                  </span>
                </div>
              ) : null}
            </section>

            <h3 className="section-title" data-testid="recon-breaks-heading">
              Transaction breaks{" "}
              {latest.run.breaks_found > 0 ? (
                <span className="muted" data-testid="recon-breaks-count">
                  ({latest.run.breaks_recorded} of {latest.run.breaks_found})
                </span>
              ) : null}
            </h3>
            <p className="sub">{latest.note}</p>

            {/* A short list reads as a complete one, which is the same
                misreading a blank count invites. The run stores at most
                `max_recorded_breaks` entries while `breaks_found` counts every
                one it found, so on a large run the table is a prefix.

                Deliberately NOT offered as pagination. The unrecorded breaks
                were never written down -- they exist inside a count and nowhere
                else -- so there is no page to fetch, and a "next" control would
                promise rows no query can produce. What an operator can act on
                is the count, and the fact that the file is where the rest are. */}
            {latest.run.breaks_truncated ? (
              <p className="alert alert-warn" data-testid="recon-breaks-truncated">
                This run found {latest.run.breaks_found} breaks and recorded the
                first {latest.run.breaks_recorded}. The remaining{" "}
                {latest.run.breaks_found - latest.run.breaks_recorded} were
                counted but not stored, so they cannot be listed here — the
                settlement file and the ledger are where they can be found.
              </p>
            ) : null}

            {latest.run.breaks.length === 0 ? (
              <div className="card empty" data-testid="recon-no-breaks">
                This run recorded no transaction breaks.
              </div>
            ) : (
              /* `table-wrap`, matching the candidate table above and every
                 other table on the site: a bare table overflows the card on a
                 narrow screen instead of scrolling inside it. */
              <div className="table-wrap">
                <table data-testid="recon-breaks-table">
                  <thead>
                    <tr>
                      <th>Loan</th>
                      <th>Processor reference</th>
                      <th>Break type</th>
                      <th>Ledger</th>
                      <th>Settlement</th>
                      <th>Difference</th>
                    </tr>
                  </thead>
                  <tbody>
                    {latest.run.breaks.map((b, i) => (
                      <tr
                        key={`${b.loan_id}-${b.processor_ref ?? "none"}-${i}`}
                        data-testid={`recon-break-${b.loan_id}`}
                      >
                        <td>
                          {/* Straight to the loan the mismatch is about.
                              Investigating a break starts by reading that
                              loan's activity. */}
                          <Link href={`/servicing/${b.loan_id}`}>
                            Loan {b.loan_id}
                          </Link>
                        </td>
                        {/* The processor's own reference. Not card data and not
                            derived from any: it is the handle both sides of the
                            comparison already key on. */}
                        <td>{b.processor_ref ?? "none recorded"}</td>
                        <td>{b.kind}</td>
                        <td className="num">${b.ledger}</td>
                        <td className="num">${b.settlement}</td>
                        <td className="num">${b.difference}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
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
