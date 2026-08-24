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
  /**
   * Opaque, non-identifying handle for the funding source.
   *
   * A real processor SDK returns one of these with the token -- a vaulted-source
   * id, called a "fingerprint" in several providers' vocabulary. It identifies
   * the instrument without describing it, which is what lets a backend ask "same
   * card?" without ever seeing a card.
   *
   * Meridian needs it for one reason: the client's decision of 2026-08-24 flags a
   * payment for human review only when the loan, the amount, the payment SOURCE
   * and the channel all match inside 30 minutes. Without a source handle the
   * heuristic would have to fall back to loan + amount + channel, which is what a
   * legitimate second installment looks like.
   */
  source_ref: string;
}

/**
 * A stable handle for a card, minted once per card per browser session.
 *
 * **Not derived from the PAN, deliberately.** Hashing the card number would be
 * the obvious way to get stability, and it would put a card-correlatable value
 * into `payments.source_ref` -- a hashed PAN is still PAN-derived data, and a
 * structured 16-digit space is not large enough for that to be comfortable. This
 * repository spent Weeks 5-8 removing exactly that class of value, so the handle
 * is a random UUID remembered against the card for the life of the session,
 * which is how a processor vault behaves from the outside.
 *
 * **The limitation, stated rather than buried.** `sessionStorage` is per tab and
 * per browser, so the same card in a new session gets a new handle: "same
 * source" is provable within a session only. That is enough for the seeded
 * fictional traffic the client asked the heuristic to be validated against, and
 * it is not a claim about production provider semantics. A real integration gets
 * a durable handle from the processor and this function goes away with the rest
 * of the mock.
 *
 * Falls back to a fresh handle when storage is unavailable (private mode, a
 * browser with site data blocked). A fresh handle means "cannot prove same
 * source", and the backend treats that as no signal rather than as a match --
 * fail-closed toward not flagging.
 */
export interface HandleStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** The browser's own session storage, or nothing when there is no browser. */
function defaultStore(): HandleStore | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/**
 * Exported, with the store injectable, so the stability rule can be tested
 * against the shipped function rather than against a copy of it. A test that
 * reimplements the logic it is checking proves only that someone can write the
 * same lines twice.
 */
export function sourceHandleFor(digits: string, store?: HandleStore | null): string {
  const fresh = `src_mock_${crypto.randomUUID()}`;
  const storage = store === undefined ? defaultStore() : store;
  if (!storage) return fresh;

  // Keyed by last4 + length rather than by the PAN: the map lives in the
  // browser, but writing a full card number into storage would be careless even
  // there, and it is not needed to tell two synthetic test cards apart.
  const key = `meridian.src.${digits.length}.${digits.slice(-4)}`;
  try {
    const existing = storage.getItem(key);
    if (existing) return existing;
    storage.setItem(key, fresh);
    return fresh;
  } catch {
    // Storage present but refusing (site data blocked, quota exceeded). A fresh
    // handle means "cannot prove same source", and the backend treats that as no
    // signal -- fail-closed toward not flagging.
    return fresh;
  }
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
    source_ref: sourceHandleFor(digits),
  };
}
