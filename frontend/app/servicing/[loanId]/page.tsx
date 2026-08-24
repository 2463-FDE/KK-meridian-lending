"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import PaymentAllocation from "../../../components/PaymentAllocation";
import RequireRole from "../../../components/RequireRole";
import StatusChip from "../../../components/StatusChip";
import { apiGet, apiPost } from "../../../lib/api";
import { usd, pct, shortDate } from "../../../lib/format";
import { tokenizeCard } from "../../../lib/tokenize";

interface Loan {
  id: string | number;
  applicant_name: string;
  principal: number;
  // Servicing exposes the CONTRACTUAL note rate under its accurate name, and
  // since the D19 contract step (db/migrations/0039) the database column is
  // called `note_rate_pct` too -- the API is no longer where a legacy name
  // stops. This is NOT the disclosed federal APR: that lives on the offer, and
  // is the larger of the two once a prepaid fee exists.
  note_rate_pct: number | null;
  note_rate_proven?: boolean;
  term_months: number;
  status: string;
  balance: number;
  past_due: number;
  opened_at: string;
}

interface ScheduleRow {
  n: number;
  due_date: string;
  payment: number;
  principal: number;
  interest: number;
  balance: number;
}

interface PaymentRow {
  id: string | number;
  amount: number;
  method: string;
  created_at: string;
  masked_pan?: string | null;
  // What this payment actually paid, read by servicing from the ledger entries
  // that moved the balance (`_allocations_by_payment`). `null` means there is no
  // ledger evidence for this payment -- a historical one applied before the
  // ledger existed -- and is NOT the same fact as 0.00, which means the
  // component received nothing. `lib/allocation.ts` keeps the two apart; see the
  // note there about `usd(null)` rendering as "$0.00".
  applied_to_fees?: number | null;
  applied_to_interest?: number | null;
  applied_to_principal?: number | null;
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

/**
 * What to say after a proposal is raised.
 *
 * The old copy said "Balance adjusted to $X." / "Fee of $X waived." Since #35
 * neither call moves money: they return 202 with `balance_moved: false` and a
 * pending movement id. Telling an operator the balance changed, when it has
 * not and may never, is the worst version of this wrong -- they would stop
 * chasing the approval the change actually depends on.
 *
 * The movement id is included because it is the thing an operator quotes when
 * asking a colleague to review it.
 */
function proposalMsg(proposal: unknown): string {
  const id =
    proposal && typeof proposal === "object" && "movement_id" in proposal
      ? String((proposal as { movement_id: unknown }).movement_id)
      : null;
  return (
    (id ? `Proposal ${id} raised. ` : "Proposal raised. ") +
    "No money has moved — a different member of staff has to approve it first."
  );
}

export default function LoanDetailPage() {
  // Shared page: staff reach it from /servicing's portfolio list, borrowers
  // reach it from /my-loan's "View account & make a payment" link -- so
  // "borrower" must be allowed here too. Ownership is enforced server-side
  // (gateway/app/main.py's owner-or-staff checks on /lss/loans/{id} and
  // POST /payments); the "Servicing rep actions" panel below stays hidden
  // from non-staff via canRepActions regardless.
  return (
    <RequireRole allow={["borrower", "csr", "underwriter", "admin"]}>
      <LoanDetailContent />
    </RequireRole>
  );
}

function LoanDetailContent() {
  const params = useParams<{ loanId: string }>();
  const loanId = params?.loanId;

  const [loan, setLoan] = useState<Loan | null>(null);
  const [schedule, setSchedule] = useState<ScheduleRow[]>([]);
  // Where the rows came from. "contract" = the payment amounts stored on the
  // loan at boarding. "reconstructed" = solved now from principal, rate and
  // term because no schedule was ever recorded (a pre-0030 loan).
  //
  // Reviewed finding: the server has reported this since the Model B work, and
  // this page ignored it -- so a reconstruction was rendered under the heading
  // "Amortization schedule" exactly like a contractual one. A reader could not
  // tell an estimate from the agreed terms, which is the one thing the server
  // went to the trouble of saying.
  const [scheduleSource, setScheduleSource] = useState<string | null>(null);
  const [scheduleNote, setScheduleNote] = useState<string | null>(null);
  const [payments, setPayments] = useState<PaymentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSchedule, setShowSchedule] = useState(false);

  // action panels
  const [payAmount, setPayAmount] = useState("250.00");
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  // A signed DELTA on a named component, never a target balance. `AdjustIn`
  // dropped `new_balance` outright in #35 (spec 0002 REQ-VAL-1): "set the
  // balance to 250.00" cannot be reviewed without knowing what it is now, and
  // what it is now can change between the review and the approval. This form
  // sent the retired field until this commit, so every attempt 422'd.
  const [adjustComponent, setAdjustComponent] = useState<"principal" | "fees">("principal");
  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [waiveAmount, setWaiveAmount] = useState("");
  const [waiveReason, setWaiveReason] = useState("");
  // POST /payments now requires an idempotency_key (review fix -- a retry or
  // a double-click used to double-charge). Minted once and reused across
  // retries of the SAME submit attempt; a fresh one is minted only after the
  // server confirms the balance was actually applied ("captured"), so a
  // "pending" response (charged, balance apply not yet confirmed) keeps the
  // same key on the next retry instead of starting a new, undetectable charge.
  const [payIdempotencyKey, setPayIdempotencyKey] = useState(() => crypto.randomUUID());

  // Who SEES the proposal forms, decided from the VERIFIED principal.
  //
  // Two things were wrong with computing this from `getUser()`, and the first
  // is the one that matters. This page is reachable by borrowers -- they arrive
  // from /my-loan's "View account" link, which is why `RequireRole` admits them
  // -- and `getUser()` reads localStorage. A borrower who edits
  // `meridian.user.role` to "csr" was shown the money forms. Nothing moved:
  // the gateway refuses a non-staff caller on these routes regardless. But
  // offering a control the caller has no authority to use is the same defect
  // the approvals queue was corrected for (APQ-003), and it is worse here,
  // because the page is one borrowers are meant to be on.
  //
  // The second: the set was wrong. `specs/0002` §"role matrix" grants "Raise a
  // proposal" to csr, underwriter AND admin -- "any staff member may ask" --
  // and both server layers agree (`gateway`'s `_PROPOSAL_ACTIONS` branch admits
  // any staff principal, `maker_checker.PROPOSER_ROLES` is all three). Only the
  // browser disagreed, so an underwriter was denied a capability the
  // specification of record grants and the server would have allowed.
  //
  // These are not two changes. They are one expression that has to be right:
  // the proposer set, read from an identity the caller cannot edit.
  const [canRepActions, setCanRepActions] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = (await apiGet("/auth/me")) as { role?: string };
        if (cancelled) return;
        // Mirrors maker_checker.PROPOSER_ROLES. `late-fee` is NOT offered on
        // this page, so this set covers proposals only -- it must not be
        // reused for a control that moves money on one person's say-so.
        setCanRepActions(
          me.role === "csr" || me.role === "underwriter" || me.role === "admin");
      } catch {
        // Unverifiable identity offers nothing. Failing closed is the whole
        // point: the previous version failed OPEN on whatever the cache said.
        if (!cancelled) setCanRepActions(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const loadAll = useCallback(async () => {
    if (!loanId) return;
    setLoading(true);
    setError(null);
    try {
      // Load loan first; schedule/payments are best-effort (tolerate failures).
      const l = (await apiGet(`/lss/loans/${loanId}`)) as Loan;
      setLoan(l);
      const [sch, pay] = await Promise.allSettled([
        apiGet(`/lss/loans/${loanId}/schedule`),
        apiGet(`/lss/loans/${loanId}/payments`),
      ]);
      if (sch.status === "fulfilled") {
        const body = sch.value as {
          schedule?: ScheduleRow[];
          source?: string;
          note?: string;
        };
        setSchedule(body?.schedule ?? []);
        setScheduleSource(body?.source ?? null);
        setScheduleNote(body?.note ?? null);
      }
      if (pay.status === "fulfilled") {
        setPayments((pay.value as { items?: PaymentRow[] })?.items ?? []);
      }
    } catch (err) {
      setError(errMsg(err, "Could not load this loan."));
      setLoan(null);
    } finally {
      setLoading(false);
    }
  }, [loanId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Refresh only balance + payment history after an action.
  const refreshBalanceAndHistory = useCallback(async () => {
    if (!loanId) return;
    const [bal, pay] = await Promise.allSettled([
      apiGet(`/lss/accounts/${loanId}/balance`),
      apiGet(`/lss/loans/${loanId}/payments`),
    ]);
    if (bal.status === "fulfilled") {
      const b = bal.value as { balance?: number; past_due?: number };
      setLoan((prev) =>
        prev
          ? {
              ...prev,
              balance: b.balance ?? prev.balance,
              past_due: b.past_due ?? prev.past_due,
            }
          : prev
      );
    }
    if (pay.status === "fulfilled") {
      setPayments((pay.value as { items?: PaymentRow[] })?.items ?? []);
    }
  }, [loanId]);

  async function makePayment() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // ADR 0008 (Week 5 tokenization): the card never leaves the browser as
      // a raw PAN/CVV -- tokenizeCard() (a mock standing in for a real
      // processor SDK) returns only an opaque token + display fields, which
      // is all payment-service's own schema even accepts anymore.
      const card = "4111111111111111"; // hardcoded test card (texture)
      const token = tokenizeCard(card, "123");
      const resp = (await apiPost("/payments", {
        loan_id: loanId,
        processor_token: token.processor_token,
        last4: token.last4,
        brand: token.brand,
        amount: parseFloat(payAmount || "0"),
        method: "card",
        idempotency_key: payIdempotencyKey,
      })) as { status?: string };

      // Review fix: payment-service deliberately returns HTTP 200 with
      // status: "pending" when the charge captured but applying it to the
      // balance failed/hasn't been confirmed yet -- that is NOT success. Only
      // rotate the key once the server confirms "captured"; on "pending" keep
      // the same key so a retry reconciles the SAME payment instead of the
      // server having no way to tell it apart from a brand-new charge.
      if (resp?.status === "captured") {
        setActionMsg(`Payment of ${usd(payAmount)} submitted.`);
        setPayIdempotencyKey(crypto.randomUUID());
      } else {
        setActionMsg(
          `Payment of ${usd(payAmount)} is pending -- click "Pay with card on file" again to retry.`
        );
      }
      await refreshBalanceAndHistory();
    } catch (err) {
      setActionErr(errMsg(err, "Payment failed."));
    } finally {
      setActionBusy(false);
    }
  }

  async function adjustBalance() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // Gateway now enforces staff-only here regardless of who calls it.
      const proposal = await apiPost(`/lss/accounts/${loanId}/adjust-balance`, {
        component: adjustComponent,
        amount: parseFloat(adjustAmount || "0"),
        reason: adjustReason.trim(),
      });
      setActionMsg(proposalMsg(proposal));
      // Deliberately NOT refreshing the balance: nothing moved. Re-reading it
      // here would show the same figure next to a success message, which reads
      // as "the adjustment was applied and made no difference".
      setAdjustAmount("");
      setAdjustReason("");
    } catch (err) {
      setActionErr(errMsg(err, "The adjustment could not be proposed."));
    } finally {
      setActionBusy(false);
    }
  }

  async function waiveFee() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // Gateway now enforces staff-only here regardless of who calls it.
      //
      // Negated here, and this is a presentation choice rather than a rule of
      // our own: a waiver REDUCES what the borrower owes, so the API requires a
      // negative amount and refuses a positive one outright. Asking an operator
      // to type "-25" to waive $25 invites the sign error the refusal exists to
      // catch, so the field takes what they mean and the client sends what the
      // contract states.
      const entered = parseFloat(waiveAmount || "0");
      const proposal = await apiPost(`/lss/accounts/${loanId}/waive-fee`, {
        amount: entered > 0 ? -entered : entered,
        reason: waiveReason.trim(),
      });
      setActionMsg(proposalMsg(proposal));
      setWaiveAmount("");
      setWaiveReason("");
    } catch (err) {
      setActionErr(errMsg(err, "The fee waiver could not be proposed."));
    } finally {
      setActionBusy(false);
    }
  }

  if (loading && !loan) {
    return (
      <main className="wrap">
        <p className="muted">Loading loan #{loanId}…</p>
      </main>
    );
  }

  if (error && !loan) {
    return (
      <main className="wrap">
        <p>
          <Link href="/servicing">← Back to servicing</Link>
        </p>
        <div className="alert alert-error">{error}</div>
      </main>
    );
  }

  return (
    <main className="wrap">
      <p style={{ marginBottom: 12 }}>
        <Link href="/servicing">← Back to servicing</Link>
      </p>

      {/* Header */}
      <div className="spread">
        <div>
          <h1 style={{ marginBottom: 6 }}>
            {loan?.applicant_name || "Loan account"}
          </h1>
          <p className="sub" style={{ margin: 0 }}>
            Loan #{String(loanId)}
          </p>
        </div>
        {loan ? <StatusChip status={loan.status} /> : null}
      </div>

      {/* Balance / terms cards */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Current balance</div>
          <div className="kpi-value">{usd(loan?.balance)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Past due</div>
          <div className={`kpi-value${(loan?.past_due ?? 0) > 0 ? " danger" : ""}`}>
            {usd(loan?.past_due)}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Opened</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {shortDate(loan?.opened_at)}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>
          Loan terms
        </div>
        <div className="dl">
          <div className="dl-row">
            <dt>Original principal</dt>
            <dd>{usd(loan?.principal)}</dd>
          </div>
          <div className="dl-row">
            <dt>Interest rate (note rate)</dt>
            {/* The fallback is defensive, not expected. It used to be the
                normal case for a pre-0030 loan, where `loans.apr` held the
                DISCLOSED APR and naming it the note rate would have been
                wrong. `db/migrations/0039` made `note_rate_pct` NOT NULL, so
                the API has a rate for every loan -- this branch now only
                catches a response that is missing the field entirely. */}
            <dd>
              {loan?.note_rate_pct != null
                ? pct(loan.note_rate_pct)
                : "Not recorded (legacy loan)"}
            </dd>
          </div>
          <div className="dl-row">
            <dt>Term</dt>
            <dd>{loan?.term_months} months</dd>
          </div>
          <div className="dl-row">
            <dt>Status</dt>
            <dd>{loan ? <StatusChip status={loan.status} /> : "—"}</dd>
          </div>
        </div>
      </div>

      {/* Amortization schedule */}
      <h2>
        Amortization schedule
        {scheduleSource === "reconstructed" ? " (reconstructed)" : null}
      </h2>
      {/* The qualification goes ABOVE the table and outside the collapsed
          section: a caveat inside a panel the reader has to expand is a caveat
          they can miss, and this one changes what the numbers mean. */}
      {scheduleNote ? (
        <div
          className={
            scheduleSource === "reconstructed" ? "alert alert-warn" : "alert alert-error"
          }
          data-testid="schedule-note"
          style={{ marginBottom: 12 }}
        >
          <strong>
            {scheduleSource === "reconstructed"
              ? "These are not the agreed terms."
              : "This loan's recorded terms do not add up."}
          </strong>{" "}
          {scheduleNote}
        </div>
      ) : null}
      {schedule.length === 0 ? (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No schedule available for this loan.
          </p>
        </div>
      ) : (
        <>
          <button
            className="collapse-toggle"
            onClick={() => setShowSchedule((v) => !v)}
          >
            {showSchedule ? "Hide" : "Show"} schedule ({schedule.length} payments)
          </button>
          {showSchedule ? (
            <div className="table-wrap table-scroll" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Due date</th>
                    <th className="num">Payment</th>
                    <th className="num">Principal</th>
                    <th className="num">Interest</th>
                    <th className="num">Remaining balance</th>
                  </tr>
                </thead>
                <tbody>
                  {schedule.map((r) => (
                    <tr key={r.n}>
                      <td>{r.n}</td>
                      <td>{shortDate(r.due_date)}</td>
                      <td className="num">{usd(r.payment)}</td>
                      <td className="num">{usd(r.principal)}</td>
                      <td className="num">{usd(r.interest)}</td>
                      <td className="num">{usd(r.balance)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      )}

      {/* Payment history */}
      <h2>Payment history</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Method</th>
              <th>Card</th>
              <th className="num">Amount</th>
              <th>Applied to</th>
            </tr>
          </thead>
          <tbody>
            {payments.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty">
                  No payments recorded yet.
                </td>
              </tr>
            ) : (
              payments.map((p) => (
                <tr key={String(p.id)}>
                  <td>{shortDate(p.created_at)}</td>
                  <td style={{ textTransform: "capitalize" }}>{p.method}</td>
                  {/* Bug fix: this used to fall back to the literal string
                      "ACH" whenever masked_pan was empty -- but that's also
                      true for any CARD payment with no pan on record (every
                      payment, once Week 5 tokenization ships and no raw PAN
                      is ever stored again), mislabeling it as an ACH payment
                      it never was. `method` (shown in the column to the
                      left) is the actual source of truth for card vs. ACH --
                      this column only ever shows real card-on-file data, or
                      an honest "not on file" placeholder, never a guess. */}
                  <td>{p.masked_pan || "—"}</td>
                  <td className="num">{usd(p.amount)}</td>
                  {/* What the payment paid, straight from the API's own
                      figures. The client asked at the 2026-08-19 demo whether a
                      borrower can tell what a payment was applied to; the
                      columns to the left never answered it.

                      Nothing here is computed: no waterfall, no split of
                      `p.amount`, no reading of the amortization schedule. The
                      ledger entries that moved the balance are the only faithful
                      answer, and servicing already reports them. */}
                  <td>
                    <PaymentAllocation payment={p} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Action feedback (shared by all panels) */}
      {actionMsg ? <div className="alert alert-success">{actionMsg}</div> : null}
      {actionErr ? <div className="alert alert-error">{actionErr}</div> : null}

      {/* Make a payment */}
      <h2>Make a payment</h2>
      <div className="card">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field">
            {/* Associated with `htmlFor`/`id` rather than left as a loose
                sibling: an unassociated label is read out by nothing, and this
                is the one control on the borrower's own payment path. */}
            <label htmlFor="pay-amount">Amount (USD)</label>
            <input
              id="pay-amount"
              type="number"
              min="0"
              step="0.01"
              value={payAmount}
              onChange={(e) => {
                setPayAmount(e.target.value);
                setPayIdempotencyKey(crypto.randomUUID());
              }}
            />
          </div>
          <button onClick={makePayment} disabled={actionBusy}>
            {actionBusy ? "Processing…" : "Pay with card on file"}
          </button>
        </div>
        <p className="hint" style={{ marginTop: 10 }}>
          Charged to card ending 1111. Payments post immediately.
        </p>
      </div>

      {/* Proposal actions — shown to the verified staff proposal roles (csr,   */}
      {/* underwriter, admin: specs/0002's role matrix and PROPOSER_ROLES), and  */}
      {/* the gateway backs that up: /lss/accounts/{id}/adjust-balance|waive-fee */}
      {/* are staff-only server-side. Neither raises money — both propose.       */}
      {canRepActions ? (
        <>
          <h2>Servicing rep actions</h2>
          <p className="hint">
            Both actions raise a proposal for someone else to approve. Neither
            moves money on its own, and you cannot approve your own.
          </p>
          <div className="grid grid-2">
            <div className="card">
              <div className="card-title" style={{ marginBottom: 10 }}>
                Propose a balance adjustment
              </div>
              <label htmlFor="adjust-component">Component</label>
              <select
                id="adjust-component"
                value={adjustComponent}
                onChange={(e) =>
                  setAdjustComponent(e.target.value as "principal" | "fees")
                }
              >
                {/* The API accepts exactly these two for an adjustment
                    (maker_checker.COMPONENTS_BY_TYPE). `interest` is absent
                    deliberately -- it is accrued, not adjusted. */}
                <option value="principal">Principal</option>
                <option value="fees">Fees</option>
              </select>
              <label htmlFor="adjust-amount" style={{ marginTop: 10 }}>
                Change (USD)
              </label>
              <input
                id="adjust-amount"
                type="number"
                step="0.01"
                value={adjustAmount}
                onChange={(e) => setAdjustAmount(e.target.value)}
                placeholder="0.00"
              />
              <p className="hint">
                A change, not a new total. Positive increases what the borrower
                owes; negative reduces it.
              </p>
              <label htmlFor="adjust-reason" style={{ marginTop: 10 }}>
                Reason
              </label>
              <input
                id="adjust-reason"
                type="text"
                value={adjustReason}
                onChange={(e) => setAdjustReason(e.target.value)}
                placeholder="Why this adjustment is correct"
              />
              <button
                className="btn-ghost btn-block"
                style={{ marginTop: 14 }}
                onClick={adjustBalance}
                /* Reason is required and immutable once written -- the approver
                   is otherwise asked to authorise a number with no account of
                   why. Blocked here so the operator does not lose the amount
                   they typed to a 422. */
                disabled={actionBusy || !adjustAmount || !adjustReason.trim()}
              >
                Submit for approval
              </button>
            </div>
            <div className="card">
              <div className="card-title" style={{ marginBottom: 10 }}>
                Propose a fee waiver
              </div>
              <label htmlFor="waive-amount">Waiver amount (USD)</label>
              <input
                id="waive-amount"
                type="number"
                step="0.01"
                min="0"
                value={waiveAmount}
                onChange={(e) => setWaiveAmount(e.target.value)}
                placeholder="0.00"
              />
              <p className="hint">
                How much to take off the borrower&apos;s fees. Enter it as a
                positive figure.
              </p>
              <label htmlFor="waive-reason" style={{ marginTop: 10 }}>
                Reason
              </label>
              <input
                id="waive-reason"
                type="text"
                value={waiveReason}
                onChange={(e) => setWaiveReason(e.target.value)}
                placeholder="Why this fee should be waived"
              />
              <button
                className="btn-ghost btn-block"
                style={{ marginTop: 14 }}
                onClick={waiveFee}
                disabled={actionBusy || !waiveAmount || !waiveReason.trim()}
              >
                Submit for approval
              </button>
            </div>
          </div>
        </>
      ) : null}
    </main>
  );
}
