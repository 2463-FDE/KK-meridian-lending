// Where the note rate comes from: the server, not this browser.
//
// Both the apply flow and the underwriting screen used to hold
// `const OFFER_RATE_PCT = 7.99` and post it into offer creation, which made the
// contractual rate on a real loan whatever the client sent. The same number also
// lived in origination's request schema and in disclosure-service's, so a
// contractual term had five copies and the one that reached the borrower's loan
// was the one furthest from any authority.
//
// The server owns it now (`origination-service/app/config.py::DEMO_NOTE_RATE_PCT`)
// and answers `GET /los/pricing`. This module is the read path, and it exists so
// no screen is tempted to keep its own copy for display purposes.
//
// **It is a training default, not a pricing policy.** There is one rate, it
// applies to every offer, and nothing in this system underwrites a per-applicant
// rate -- not by score, income, DTI, employment, or anything a model produces.
// The response carries that fact in its own fields, and `describePricing` below
// is what puts it in front of a reader rather than leaving it in a JSON payload.

import { apiGet } from "./api";

export interface Pricing {
  note_rate_pct: number;
  source: string;
  is_production_pricing_policy: boolean;
  note?: string;
}

/**
 * Read the configured note rate.
 *
 * Returns null on failure rather than falling back to a number. A hardcoded
 * fallback is how the browser came to own pricing in the first place, and a
 * screen that cannot reach the server should say so instead of displaying a rate
 * nobody confirmed -- see `describePricing`.
 */
export async function fetchPricing(): Promise<Pricing | null> {
  try {
    const res = (await apiGet("/los/pricing")) as Pricing;
    if (typeof res?.note_rate_pct !== "number") return null;
    return res;
  } catch {
    return null;
  }
}

/**
 * How to describe the rate to a reader, including when it is unknown.
 *
 * Separated from the fetch so the wording is testable without a network, and so
 * the "we could not read it" case is a first-class answer rather than an empty
 * string that renders as a gap.
 */
export function describePricing(pricing: Pricing | null): string {
  if (!pricing) {
    return "Your rate is set when the offer is generated.";
  }
  return `${pricing.note_rate_pct.toFixed(2)}%`;
}
