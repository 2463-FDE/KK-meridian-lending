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
  | { kind: "unavailable" };

const COMPONENTS: { label: string; key: keyof PaymentAllocationFields }[] = [
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

  if (lines.length === 0) return { kind: "unavailable" };
  return { kind: "known", lines, unknownLabels };
}
