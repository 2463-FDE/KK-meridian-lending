// Money + percentage formatting helpers — used across the whole app so that
// every dollar figure renders identically (consumer-lending convention).

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/** Format a number as USD, e.g. 12345.6 -> "$12,345.60". Tolerates undefined/null/strings. */
export function usd(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (n === null || n === undefined || Number.isNaN(n as number)) return "$0.00";
  return USD.format(n as number);
}

/** Format a numeric rate as a percentage, e.g. 7.99 -> "7.99%". */
export function pct(value: number | string | null | undefined, digits = 2): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (n === null || n === undefined || Number.isNaN(n as number)) return "—";
  return `${(n as number).toFixed(digits)}%`;
}

/** Format an ISO date / date string as a short US date, e.g. "May 25, 2026". */
export function shortDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * Describe a Model B payment plan in words.
 *
 * Under Model B (db/migrations/0030) a loan bills `regularPayment` for
 * `regularPaymentCount` periods and a different `finalPayment` in the last one,
 * which absorbs the cent residue. The UI used to render "monthly payment $X"
 * and a term, which told the borrower they would make N identical payments --
 * not what the contract says, and off by the final adjustment.
 *
 * Four cases, all of them real:
 *
 *  - No stored schedule (a pre-0030 offer): fall back to the single monthly
 *    figure. Deliberately NOT a reconstructed final payment -- shown beside
 *    genuinely disclosed amounts, a computed one is indistinguishable from a
 *    disclosed one.
 *  - The final payment equals the regular one (a term where the rounding
 *    happens to come out even, and every zero-residue schedule): say so as one
 *    uniform series rather than drawing attention to a distinction that has no
 *    consequence here.
 *  - Exactly one regular payment: "1 payment", not "1 payments".
 *  - The general case: N payments then a final payment.
 */
export function paymentPlanText(
  regularPayment: number,
  regularPaymentCount?: number | null,
  finalPayment?: number | null,
): string {
  if (regularPaymentCount == null || finalPayment == null) {
    return `monthly payment ${usd(regularPayment)}`;
  }
  // Compared in cents: these arrive as JSON numbers, and 407.12 === 407.12 is
  // only reliable because both sides came from the same 2dp source. Rounding
  // first makes that explicit rather than relying on it.
  const sameAmount = Math.round(regularPayment * 100) === Math.round(finalPayment * 100);
  const total = regularPaymentCount + 1;
  if (sameAmount) {
    return `${total} monthly payments of ${usd(regularPayment)}`;
  }
  const series =
    regularPaymentCount === 1
      ? `1 monthly payment of ${usd(regularPayment)}`
      : `${regularPaymentCount} monthly payments of ${usd(regularPayment)}`;
  return `${series}, followed by one final payment of ${usd(finalPayment)}`;
}
