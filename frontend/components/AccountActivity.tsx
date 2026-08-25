"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { usd, shortDate } from "../lib/format";

/**
 * The authoritative movements that changed one loan account.
 *
 * A different question from payment history, and the difference is the reason
 * this exists: payment history asks "what payments did I make, and where did
 * each one go", while activity asks "what movements changed this account". An
 * approved adjustment and a fee waiver change what is owed without being
 * payments, and a proposal nobody approved changes nothing and appears in
 * neither.
 *
 * **Nothing here computes money.** Every figure is rendered as the server sent
 * it (`GET /lss/loans/{id}/activity`, read from the immutable ledger). The
 * components of a payment are not summed to check the total, no balance is
 * derived by subtracting a movement from another, and no waterfall is applied --
 * the server owns fees → interest → principal and this is a view of what it
 * already did.
 *
 * **The sign is the information.** The server sends the ledger's own convention:
 * negative reduces what is owed, positive increases it. That is the same
 * convention the staff adjustment form uses, so +450 means the same thing on
 * both screens. Payment history flips signs for readability; this deliberately
 * does not.
 */

interface ActivityItem {
  id: string;
  occurred_at: string | null;
  category: string;
  description: string;
  amount: number;
  components: Record<string, number>;
  payment_id: number | null;
  provenance: string;
}

interface ActivityResponse {
  loan_id: number;
  items?: ActivityItem[];
  note?: string;
}

/** Which component a line refers to, in the borrower's words. */
const COMPONENT_LABEL: Record<string, string> = {
  fees: "Fees",
  interest: "Interest",
  principal: "Principal",
};

/**
 * What a thin provenance means, said plainly.
 *
 * Only shown for `limited`. The server marks a movement that way when the record
 * genuinely cannot name an actor or a reason -- an opening balance from when the
 * ledger began, or a change captured by a database trigger from a direct write
 * that predates it. Saying so is better than presenting it beside fully
 * evidenced movements with nothing to distinguish it.
 */
const LIMITED_NOTE =
  "Recorded before this account's full history was kept, so its origin is not detailed.";

export default function AccountActivity({
  loanId,
  heading = "Account activity",
  reloadKey = 0,
}: {
  loanId: string | number;
  heading?: string;
  /**
   * Bumped by the parent when something on the page has changed the account.
   *
   * Review of PR #87 (AA-FRESH-001): this fetched on mount only, so a payment
   * the user had just captured appeared in payment history -- which the page
   * re-reads -- and NOT in activity, sitting stale beside it. Two panels on one
   * screen disagreeing about the same account is worse than either being
   * absent, because both look authoritative.
   *
   * A counter rather than a callback ref: the parent already knows when it has
   * changed something, and an effect dependency is the smallest thing that
   * cannot be forgotten halfway. A parent that never bumps it behaves exactly as
   * before.
   */
  reloadKey?: number;
}) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = (await apiGet(
        `/lss/loans/${loanId}/activity`,
      )) as ActivityResponse;
      setItems(body.items ?? []);
      // The server's sentence about what this list is, displayed rather than
      // rewritten here: a paraphrase in the browser is a second copy of the
      // statement, free to soften as the page is edited.
      setNote(body.note ?? null);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "This account's activity could not be loaded.",
      );
    } finally {
      setLoading(false);
    }
  }, [loanId]);

  useEffect(() => {
    load();
    // `reloadKey` is a dependency, not an argument: `load` is memoised on
    // `loanId`, so without this the effect never re-runs for the same loan.
  }, [load, reloadKey]);

  return (
    <section className="card" style={{ marginTop: 20 }} data-testid="account-activity">
      <div className="card-title" style={{ marginBottom: 6 }}>
        {heading}
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        {note ??
          "Authoritative movements that changed this account, read from the immutable ledger."}
      </p>

      {error ? <p className="alert alert-error">{error}</p> : null}

      {loading ? (
        <p className="muted">Loading…</p>
      ) : error ? (
        /* Not "no activity". A list that claims to be empty on the strength of
           a request that failed is the same defect as two equal reconciliation
           totals with no run behind them. */
        <p className="muted">
          This activity could not be read, so this is not a statement that
          nothing has happened.
        </p>
      ) : items.length === 0 ? (
        <p className="muted">No movements have been recorded on this account.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>What</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                // One payment is one row, however many ledger components it
                // wrote. The server did the grouping, by payment id -- three
                // rows here would be three charges the borrower never made.
                const parts = Object.entries(item.components).filter(
                  ([, value]) => value !== 0,
                );
                return (
                  <tr key={item.id} data-testid={`activity-${item.id}`}>
                    <td>{item.occurred_at ? shortDate(item.occurred_at) : "—"}</td>
                    <td>
                      {item.description}
                      {item.payment_id != null ? (
                        <span className="muted"> · payment {item.payment_id}</span>
                      ) : null}
                      {parts.length > 1 ? (
                        <div className="muted" style={{ marginTop: 4 }}>
                          {/* Displayed, not summed. `usd(Math.abs(...))` is
                              formatting: the direction is already stated by the
                              movement's own amount beside it, and a minus sign
                              on each part would read as three separate debits. */}
                          Applied to{" "}
                          {parts
                            .map(
                              ([component, value]) =>
                                `${COMPONENT_LABEL[component] ?? component} ${usd(
                                  Math.abs(value),
                                )}`,
                            )
                            .join(" · ")}
                        </div>
                      ) : null}
                      {item.provenance === "limited" ? (
                        <div className="muted" style={{ marginTop: 4 }}>
                          {LIMITED_NOTE}
                        </div>
                      ) : null}
                    </td>
                    <td className={`num ${item.amount > 0 ? "danger-text" : ""}`}>
                      {item.amount > 0 ? "+" : "−"}
                      {usd(Math.abs(item.amount))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
