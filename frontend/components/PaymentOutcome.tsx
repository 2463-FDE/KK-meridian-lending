"use client";

import { usd } from "../lib/format";

/**
 * What happened to the payment that was just submitted, and only that one.
 *
 * Three outcomes, told apart because the server tells them apart
 * (`PaymentOut.status`) and they mean different things to a borrower:
 *
 *   * **captured** — the processor confirmed the charge AND servicing confirmed
 *     it was applied to the loan. This is the only state in which a receipt is
 *     truthful;
 *   * **pending** — the processor confirmed, Meridian has not yet confirmed the
 *     application. No balance may be claimed;
 *   * **failed** — the processor declined. Nothing was applied, and saying
 *     "pending" here invites a retry that cannot succeed, because a declined
 *     idempotency key stays declined.
 *
 * **The receipt is matched by `payment_id`.** The charge response carries the
 * server's own id, and that is the only safe way to name the payment that just
 * completed: two legitimate payments can share an amount, a timestamp and a
 * card, so "the newest row" or "the row with this amount" can describe someone
 * else's money.
 *
 * **Nothing here computes money.** The amount and the split come from the
 * payment-history row, which servicing reads from the ledger entries that moved
 * the balance. The principal comes from a refreshed server read. There is no
 * `oldBalance - amount` anywhere, because fees and interest consume part of a
 * payment and that subtraction would be wrong whenever they do.
 */

interface PaymentRowLike {
  id: string | number;
  amount: number;
  applied_to_fees?: number | null;
  applied_to_interest?: number | null;
  applied_to_principal?: number | null;
}

export interface PaymentAttempt {
  status: "captured" | "pending" | "failed";
  paymentId: number | null;
  amount: number;
}

export default function PaymentOutcome({
  result,
  payments,
  currentPrincipal,
}: {
  result: PaymentAttempt;
  payments: PaymentRowLike[];
  currentPrincipal: number | null;
}) {
  if (result.status === "failed") {
    return (
      <div className="alert alert-error" data-testid="payment-declined">
        <strong>Payment declined.</strong> The card was declined and{" "}
        <strong>no payment was applied to the loan</strong>. Nothing was charged.
        You can try again — the next attempt is treated as a new payment.
      </div>
    );
  }

  if (result.status === "pending") {
    return (
      <div className="alert" data-testid="payment-pending">
        {/* "Captured", not "pending", is the first word on purpose. Two
            different things can be outstanding after a card is presented: the
            CHARGE, and the APPLICATION of that charge to the loan. Only the
            second is unresolved here, and "Payment pending" left a reader to
            guess which -- someone reading it as "the card has not gone through"
            would reasonably try again on a new key and authorise a second
            charge. The heading now says which half is settled. */}
        <strong>Captured — allocation pending.</strong> The payment processor has
        confirmed the transaction, but Meridian has not yet confirmed that it has
        been applied to this loan, so no allocation is shown — an allocation is
        read from ledger evidence that does not exist yet, and estimating one
        here would be a guess presented as a receipt. The balance below has not
        changed yet.{" "}
        <strong>
          Use &ldquo;Pay with card on file&rdquo; again to check on this same
          payment
        </strong>{" "}
        — it will not charge you twice.
      </div>
    );
  }

  // Captured. Matched by id, never by amount or recency.
  const row = payments.find(
    (p) => result.paymentId != null && String(p.id) === String(result.paymentId),
  );

  // `captured` means servicing confirmed the application, so a ledger-backed
  // split should exist. If the refreshed read cannot supply one, say so rather
  // than rendering three zeros: `null` means "no allocation evidence" and 0.00
  // means "this component received nothing", and turning the first into the
  // second would state a fact nobody recorded.
  const split =
    row &&
    row.applied_to_fees != null &&
    row.applied_to_interest != null &&
    row.applied_to_principal != null
      ? {
          fees: row.applied_to_fees,
          interest: row.applied_to_interest,
          principal: row.applied_to_principal,
        }
      : null;

  return (
    <div className="alert alert-success" data-testid="payment-posted">
      <div>
        <strong>Payment posted.</strong>{" "}
        {usd(row ? row.amount : result.amount)}
        {result.paymentId != null ? (
          <span className="muted"> · payment {result.paymentId}</span>
        ) : null}
      </div>

      {split ? (
        <dl className="dl" data-testid="payment-receipt-split">
          <div className="dl-row">
            <dt>Fees</dt>
            <dd>{usd(split.fees)}</dd>
          </div>
          <div className="dl-row">
            <dt>Interest</dt>
            <dd>{usd(split.interest)}</dd>
          </div>
          <div className="dl-row">
            <dt>Principal</dt>
            <dd>{usd(split.principal)}</dd>
          </div>
        </dl>
      ) : (
        <p data-testid="payment-receipt-unavailable">
          Payment posted, but the allocation details are not currently available.
          {result.paymentId != null
            ? ` Quote payment ${result.paymentId} if you need them looked up.`
            : ""}
        </p>
      )}

      {currentPrincipal != null ? (
        <div className="dl">
          <div className="dl-row">
            {/* "Current principal balance", not a payoff. No payoff policy
                exists in this system, so naming one would invent future
                interest, an early-payoff rule and an overpayment policy in a
                label. */}
            <dt>Current principal balance</dt>
            <dd data-testid="payment-receipt-principal">{usd(currentPrincipal)}</dd>
          </div>
        </div>
      ) : null}
    </div>
  );
}
