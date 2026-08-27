import { test, expect } from "@playwright/test";
import {
  comparisonState,
  comparisonStatement,
} from "../lib/reconciliation";

/**
 * The three states beneath the reconciliation totals, tested without a browser.
 *
 * `last_successful_run` is null in two different situations, and the panel used
 * to say the same thing in both: "The comparison has never completed, so the two
 * totals above prove nothing." That is correct when nothing has run. It is false
 * when runs HAVE executed and found breaks -- which is where this system is, and
 * it read as though the control had never run rather than as though it had run
 * and found something.
 *
 * These assertions exist so the two cases cannot collapse back into one
 * sentence. The one that matters most is the middle one, because it is the only
 * state a reader could be actively misled by.
 *
 * `shortDate` is passed in as identity here on purpose: the wording is what is
 * under test, not the locale.
 */

const asIs = (iso: string) => iso;

test("nothing has ever executed", () => {
  const peek = { last_successful_run: null, recent_failures: [] };

  expect(comparisonState(peek)).toBe("never_executed");
  const text = comparisonStatement(peek, asIs);
  expect(text).toContain("No reconciliation run has executed yet");
  expect(text).toContain("do not establish agreement");
  // It must not send a reader to breaks that do not exist.
  expect(text).not.toContain("breaks below");
});

test("runs executed and found breaks -- completed, but never matched cleanly", () => {
  const peek = {
    last_successful_run: null,
    recent_failures: [
      { outcome: "breach", breaks_found: 12 },
      { outcome: "breach", breaks_found: 12 },
    ],
  };

  expect(comparisonState(peek)).toBe("executed_without_a_clean_match");
  const text = comparisonStatement(peek, asIs);
  expect(text).toContain("No reconciliation run has completed with all records matching");
  expect(text).toContain("review the transaction-level breaks below");
  // The regression this test exists for: runs HAVE completed, so the panel may
  // not say otherwise.
  expect(text).not.toContain("never");
  expect(text).not.toContain("has executed yet");
});

test("a run matched cleanly", () => {
  const peek = {
    last_successful_run: { at: "2026-08-27", loans_compared: 3 },
    recent_failures: [{ outcome: "breach", breaks_found: 12 }],
  };

  expect(comparisonState(peek)).toBe("clean");
  expect(comparisonStatement(peek, asIs)).toBe(
    "Last compared 2026-08-27 across 3 loans.",
  );
});

test("a clean run wins even when earlier runs failed", () => {
  // Ordering matters: `last_successful_run` is the authority, and the presence of
  // older failures must not downgrade it.
  const peek = {
    last_successful_run: { at: "2026-08-27", loans_compared: 3 },
    recent_failures: [{ outcome: "error", breaks_found: 0 }],
  };
  expect(comparisonState(peek)).toBe("clean");
});

test("a missing recent_failures list is treated as nothing having run", () => {
  // The field is optional on the wire; absent must not read as "there were runs".
  expect(comparisonState({ last_successful_run: null })).toBe("never_executed");
  expect(comparisonState({ last_successful_run: null, recent_failures: null })).toBe(
    "never_executed",
  );
});

test("an errored run counts as executed, not as never run", () => {
  // A run that errored did execute. It did not match cleanly either, so it
  // belongs in the middle state rather than the first.
  const peek = {
    last_successful_run: null,
    recent_failures: [{ outcome: "error", breaks_found: 0 }],
  };
  expect(comparisonState(peek)).toBe("executed_without_a_clean_match");
});
