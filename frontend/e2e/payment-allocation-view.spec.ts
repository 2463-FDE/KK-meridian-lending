import { test, expect } from "@playwright/test";
import { allocationView } from "../lib/allocation";

/**
 * The null-vs-zero rule, tested without a browser.
 *
 * These run in the same suite as the browser specs but touch no page: the rule
 * they protect is a data-meaning rule, and it is cheaper and far more exact to
 * assert it directly than through rendered pixels. The browser spec beside this
 * one (`payment-allocation.spec.ts`) proves the borrower actually sees the
 * result; this proves the meaning cannot be corrupted on the way there.
 *
 * The rule: servicing sends `null` for a payment with no ledger evidence and
 * `0.00` for a component it knows received nothing. "We do not know" and "it
 * received nothing" are different statements about someone's money, and
 * `lib/format.ts::usd` maps null to "$0.00" -- so the distinction has to be
 * settled before any figure reaches a formatter.
 */

test("a payment with no ledger evidence is unavailable, not zero", () => {
  const view = allocationView({
    applied_to_fees: null,
    applied_to_interest: null,
    applied_to_principal: null,
  });

  expect(view.kind).toBe("unavailable");
});

test("a payment with missing allocation keys is unavailable", () => {
  // An older API build, or a response shaped before the fields existed. Absent
  // is no more "zero" than null is.
  expect(allocationView({}).kind).toBe("unavailable");
});

test("a known zero component is a line, not a silence", () => {
  const view = allocationView({
    applied_to_fees: 0,
    applied_to_interest: 75,
    applied_to_principal: 425,
  });

  expect(view.kind).toBe("known");
  if (view.kind !== "known") return;
  expect(view.lines).toEqual([
    { label: "Fees", amount: 0 },
    { label: "Interest", amount: 75 },
    { label: "Principal", amount: 425 },
  ]);
  expect(view.unknownLabels).toEqual([]);
});

test("components are reported in waterfall order", () => {
  const view = allocationView({
    applied_to_principal: 400,
    applied_to_interest: 75,
    applied_to_fees: 25,
  });

  if (view.kind !== "known") throw new Error("expected a known allocation");
  expect(view.lines.map((l) => l.label)).toEqual(["Fees", "Interest", "Principal"]);
});

test("the view reports the API's own figures and computes nothing", () => {
  // Deliberately inconsistent input: the components do not sum to any plausible
  // payment. A view that "corrected" or derived anything would have to disagree
  // with one of these numbers.
  const view = allocationView({
    applied_to_fees: 12.34,
    applied_to_interest: 0,
    applied_to_principal: 1,
  });

  if (view.kind !== "known") throw new Error("expected a known allocation");
  expect(view.lines.map((l) => l.amount)).toEqual([12.34, 0, 1]);
});

test("a partial response marks the unknown component instead of zeroing it", () => {
  // Servicing writes allocations all-or-nothing today, so this shape should not
  // arrive. It is asserted anyway: the failure mode of assuming otherwise is a
  // silent "$0.00" against a component nobody measured.
  const view = allocationView({
    applied_to_fees: null,
    applied_to_interest: 75,
    applied_to_principal: 425,
  });

  expect(view.kind).toBe("known");
  if (view.kind !== "known") return;
  expect(view.lines.map((l) => l.label)).toEqual(["Interest", "Principal"]);
  expect(view.unknownLabels).toEqual(["Fees"]);
});

test("a non-numeric figure is unknown rather than coerced", () => {
  const view = allocationView({
    // A hand-rolled fixture or a JSON string slipping through.
    applied_to_fees: "25.00" as unknown as number,
    applied_to_interest: Number.NaN,
    applied_to_principal: 400,
  });

  if (view.kind !== "known") throw new Error("expected a known allocation");
  expect(view.lines).toEqual([{ label: "Principal", amount: 400 }]);
  expect(view.unknownLabels).toEqual(["Fees", "Interest"]);
});
