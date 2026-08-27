/**
 * What the reconciliation totals do and do not establish, in one sentence.
 *
 * The panel shows a ledger total beside a settlement total, and two numbers on a
 * screen are not a reconciliation. D7 added a sentence beneath them so a reader
 * could not mistake "these look equal" for "something compared them" -- but the
 * sentence had only two branches, and the absent branch was the common one.
 *
 * `last_successful_run` means "when reconciliation last AGREED": the query
 * behind it is `WHERE outcome = 'ok'`. A run that executed, compared every
 * reference and found breaks is not `ok`, so it lands in `recent_failures` and
 * leaves `last_successful_run` null. The old sentence read that null as "the
 * comparison has never completed", which is a different claim and, when runs
 * have executed, a false one -- it describes a control that is working and
 * finding things as one that has never run.
 *
 * Three states, because there are three:
 *
 *   - nothing has ever executed;
 *   - runs executed and recorded breaks;
 *   - runs executed but recorded no breaks to look at;
 *   - a run matched cleanly.
 *
 * The second is where this system actually is, and it is the one the old wording
 * could not say.
 *
 * The third exists because the panel beneath this sentence lists only failures
 * with `breaks_found > 0` (`page.tsx`'s `brokenRuns`). A run that errored before
 * it could compare anything has no rows there, so a sentence telling the reader
 * to "review the transaction-level breaks below" would point at a list the page
 * renders as "No breaks recorded in the recent runs". Sending someone to an
 * empty list is the same kind of defect as the wording this file replaced --
 * confidently describing something that is not there.
 *
 * Note what none of these sentences claims: a break is a disagreement between
 * two records, not a finding about where money went.
 */

export type SuccessfulRun = {
  at: string;
  loans_compared: number;
};

export type FailedRun = {
  outcome: string;
  breaks_found: number;
};

export type ReconciliationPeek = {
  last_successful_run: SuccessfulRun | null;
  recent_failures?: FailedRun[] | null;
};

export type ComparisonState =
  | "clean"
  | "executed_with_breaks"
  | "executed_without_breaks"
  | "never_executed";

/** Which of the four states the peek describes. */
export function comparisonState(peek: ReconciliationPeek): ComparisonState {
  if (peek.last_successful_run) return "clean";
  const failures = peek.recent_failures ?? [];
  if (failures.length === 0) return "never_executed";
  // The same predicate `page.tsx` filters the break list with. If it ever stops
  // agreeing, this sentence starts pointing at rows that are not rendered.
  return failures.some((r) => r.breaks_found > 0)
    ? "executed_with_breaks"
    : "executed_without_breaks";
}

/**
 * The sentence to render beneath the totals.
 *
 * `formatDate` is injected rather than imported so this stays a pure string
 * decision -- the page keeps its own date formatting, and a test can assert the
 * wording without asserting a locale.
 */
export function comparisonStatement(
  peek: ReconciliationPeek,
  formatDate: (iso: string) => string,
): string {
  switch (comparisonState(peek)) {
    case "clean": {
      const run = peek.last_successful_run as SuccessfulRun;
      return `Last compared ${formatDate(run.at)} across ${run.loans_compared} loans.`;
    }
    case "executed_with_breaks":
      return (
        "No reconciliation run has completed with all records matching. " +
        "The headline totals do not establish agreement; review the " +
        "transaction-level breaks below."
      );
    case "executed_without_breaks":
      // Deliberately does not send the reader below: there is nothing there.
      return (
        "No reconciliation run has completed with all records matching, and " +
        "none recorded any breaks to review. The headline totals do not " +
        "establish agreement."
      );
    case "never_executed":
      return (
        "No reconciliation run has executed yet. The headline totals do not " +
        "establish agreement."
      );
  }
}
