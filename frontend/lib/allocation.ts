// What a payment was applied to, as the API reports it -- never as the browser
// works it out.
//
// The client asked at the 2026-08-19 demo whether a borrower can tell what a
// payment was applied to. The backend already answers it from the ledger entries
// that actually moved the balance
// (`servicing-service/app/routers/loans.py::_allocations_by_payment`), so the
// only job here is to display that answer without changing its meaning.
//
// **Nothing in this file computes an allocation.** No waterfall, no split of the
// amount, no inference from the amortization schedule. A second opinion about a
// movement that already happened could disagree with the ledger the moment a fee
// is waived or a schedule corrected, and the borrower would then be shown an
// allocation that never occurred.
//
// ## null is not zero, and this is where that gets protected
//
// The API returns `null` for a payment with no ledger evidence -- one applied
// before the ledger existed, or never applied at all -- and `0.00` for a
// component it knows received nothing. Those are different facts:
//
//   null  -> we do not know what this paid
//   0.00  -> we know this component received nothing
//
// Rendering the first as "$0.00" would turn "unknown" into a factual claim about
// the borrower's money. That is a one-character mistake to make, because
// `lib/format.ts::usd` deliberately maps null/undefined/NaN to "$0.00" for the
// ordinary case of a missing figure -- so passing an allocation straight into it
// produces exactly the false statement. Hence `AllocationLine.amount` is
// `number` and never `number | null`: an unknown component cannot reach `usd()`
// through this type at all.

export interface PaymentAllocationFields {
  /** Dollars applied to fees, or null when there is no ledger evidence. */
  applied_to_fees?: number | null;
  applied_to_interest?: number | null;
  applied_to_principal?: number | null;
  /**
   * Why an allocation is absent, when it is.
   *
   * The same vocabulary `PaymentOut.status` uses, so a history row and the
   * receipt describe one payment the same way. Optional because a caller with an
   * older response shape still renders correctly -- it simply cannot distinguish
   * the reasons, which is the behaviour that existed before these arrived.
   */
  auth_status?: "captured" | "pending" | "failed" | string | null;
  /** True once servicing confirmed the apply. */
  applied?: boolean | null;
}

/** One component of an allocation, with an amount we actually know. */
export interface AllocationLine {
  /** Borrower-facing label. Not a ledger component name. */
  label: string;
  /** The API's own figure, in dollars. Never derived here. */
  amount: number;
}

export type AllocationView =
  | {
      kind: "known";
      lines: AllocationLine[];
      /**
       * Components the API returned as null while others carried figures.
       *
       * The backend writes allocations all-or-nothing today, so this is
       * normally empty. It is modelled anyway because the alternative -- assuming
       * it cannot happen -- means a future partial response silently renders as
       * zero, which is the exact defect this module exists to prevent.
       */
      unknownLabels: string[];
    }
  /**
   * Captured by the processor, not yet applied to the loan.
   *
   * NOT the same as `unavailable`, and conflating them is the defect this
   * variant exists for: both have no ledger entries, so history told a borrower
   * whose payment was merely in flight that the details were "not available".
   * An allocation is read from ledger evidence that does not exist yet, so there
   * is nothing to show -- but the reason is knowable and worth saying.
   */
  | { kind: "pending" }
  /** Declined. Nothing was applied, and this must not read as a missing figure. */
  | { kind: "declined" }
  | { kind: "unavailable" };

/**
 * Only the three money fields, never the status ones.
 *
 * `keyof PaymentAllocationFields` used to be exactly the allocation columns, so
 * it was safe to index with. It no longer is -- `auth_status` is a string and
 * `applied` a boolean -- and widening the loop would hand `knownAmount` a value
 * it would quietly classify as "unknown". Naming the three keys keeps the
 * compiler enforcing that this loop only ever reads money.
 */
type AllocationKey = "applied_to_fees" | "applied_to_interest" | "applied_to_principal";

const COMPONENTS: { label: string; key: AllocationKey }[] = [
  // Waterfall order: fees, then accrued interest, then principal. It matches
  // `policies/fee_schedule.md` and `servicing-service/app/waterfall.py`, so the
  // borrower reads the components in the order their money was applied.
  { label: "Fees", key: "applied_to_fees" },
  { label: "Interest", key: "applied_to_interest" },
  { label: "Principal", key: "applied_to_principal" },
];

function knownAmount(value: number | null | undefined): number | null {
  // A string would arrive only from a hand-rolled fixture; treat anything that
  // is not a finite number as unknown rather than coercing it to 0.
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Turn the API's three allocation fields into something renderable.
 *
 * `unavailable` when every component is unknown -- the honest answer for a
 * historical payment with no ledger entries behind it. Otherwise `known`, with a
 * line per component the API gave a figure for, in waterfall order.
 */
export function allocationView(payment: PaymentAllocationFields): AllocationView {
  const lines: AllocationLine[] = [];
  const unknownLabels: string[] = [];

  for (const { label, key } of COMPONENTS) {
    const amount = knownAmount(payment[key]);
    if (amount === null) {
      unknownLabels.push(label);
    } else {
      lines.push({ label, amount });
    }
  }

  if (lines.length === 0) {
    // Ledger evidence decides FIRST. A payment with entries has an allocation
    // whatever its status columns say, so the status is only consulted to
    // explain an absence -- never to override figures that exist.
    if (payment.auth_status === "failed") return { kind: "declined" };
    if (payment.auth_status === "captured" && payment.applied !== true) {
      return { kind: "pending" };
    }
    return { kind: "unavailable" };
  }
  return { kind: "known", lines, unknownLabels };
}
