/**
 * Mock card tokenization (ADR 0008, Week 5 tokenization fix).
 *
 * This repo has no real payment processor integrated. In a real deployment,
 * this module is replaced by the processor's own client-side SDK (e.g. a
 * Stripe Elements hosted field) -- raw PAN/CVV would go directly from the
 * browser to the processor's own servers, never through any Meridian code at
 * all, client or backend. This mock stands in for that same boundary: it
 * runs entirely in the browser and returns only an opaque token plus
 * non-sensitive display fields. Callers must never send a raw pan/cvv to any
 * Meridian backend -- see services/payment-service/app/schemas.py::PaymentIn,
 * which no longer even has fields for them.
 */

export interface CardToken {
  processor_token: string;
  last4: string;
  brand: string;
}

function detectBrand(pan: string): string {
  if (/^4/.test(pan)) return "visa";
  if (/^(5[1-5]|2[2-7])/.test(pan)) return "mastercard";
  if (/^3[47]/.test(pan)) return "amex";
  if (/^6(?:011|5)/.test(pan)) return "discover";
  return "unknown";
}

/**
 * Tokenizes a card client-side. `cvv` is accepted only to mirror a real
 * processor call's shape (it would be sent to the processor for
 * verification, never to Meridian) -- it is read and discarded, never
 * returned, never logged, never sent anywhere by this function.
 */
export function tokenizeCard(pan: string, _cvv: string): CardToken {
  const digits = pan.replace(/\D/g, "");
  return {
    processor_token: `tok_mock_${crypto.randomUUID()}`,
    last4: digits.slice(-4),
    brand: detectBrand(digits),
  };
}
