"use client";

import { allocationView, PaymentAllocationFields } from "../lib/allocation";
import { usd } from "../lib/format";

/**
 * What one payment was applied to: fees, then interest, then principal.
 *
 * The 2026-08-19 demo asked whether a borrower can tell what a payment paid.
 * The payment history answered Date / Method / Card / Amount, which does not.
 * This renders the answer servicing already computes from the ledger entries
 * that actually moved the balance -- it never derives one.
 *
 * Two rules the copy has to hold to:
 *
 *  - **A historical payment with no ledger evidence says so.** The API sends
 *    `null`, not `0.00`, and three "$0.00" lines would tell the borrower their
 *    money went nowhere. `lib/allocation.ts` carries the detail.
 *  - **A known zero is shown as $0.00.** "You paid nothing towards fees" is a
 *    real, useful fact -- it is how a borrower sees they are not carrying one.
 *
 * Borrower-facing words, not ledger vocabulary: "Fees", "Interest",
 * "Principal", no `entry_type`, no component names, no claim about what the
 * split proves beyond where the money went.
 */
export default function PaymentAllocation({
  payment,
}: {
  payment: PaymentAllocationFields;
}) {
  const view = allocationView(payment);

  if (view.kind === "pending") {
    // The SAME words the receipt uses (`PaymentOutcome`'s pending branch), on
    // purpose. This row used to read "not available for this historical
    // payment" for a payment captured moments ago -- both wrong about the
    // payment and wrong about why. No allocation is shown because the ledger
    // evidence an allocation is read from does not exist yet.
    return (
      <span className="muted alloc-pending" data-testid="alloc-pending">
        Captured — allocation pending.
      </span>
    );
  }

  if (view.kind === "declined") {
    // Not a missing figure. A declined payment moved nothing, and leaving it in
    // the "not available" bucket invited the reading that an allocation exists
    // and simply was not loaded.
    return (
      <span className="muted alloc-declined" data-testid="alloc-declined">
        Declined — nothing applied.
      </span>
    );
  }

  if (view.kind === "unavailable") {
    return (
      <span className="muted alloc-empty">
        Allocation not available for this historical payment.
      </span>
    );
  }

  return (
    <dl className="alloc" aria-label="What this payment was applied to">
      {view.lines.map((line) => (
        <div className="alloc-row" key={line.label}>
          <dt>{line.label}</dt>
          {/* The API's figure, formatted. `usd()` is reached only with a number
              -- the type in lib/allocation.ts makes an unknown component
              unable to arrive here, because usd(null) renders "$0.00". */}
          <dd className="num">{usd(line.amount)}</dd>
        </div>
      ))}
      {view.unknownLabels.map((label) => (
        <div className="alloc-row" key={label}>
          <dt>{label}</dt>
          <dd className="muted">not recorded</dd>
        </div>
      ))}
    </dl>
  );
}
