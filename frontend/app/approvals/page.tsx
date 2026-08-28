"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import RequireRole from "../../components/RequireRole";
import { apiGet, apiPost } from "../../lib/api";
import { usd, shortDate } from "../../lib/format";

/**
 * The maker-checker queue: money movements proposed by one member of staff and
 * waiting for a different one to resolve them (ADR 0011, specs/0002).
 *
 * Why this page exists. `adjust-balance` and `waive-fee` have raised proposals
 * since the cutover, and the database has refused self-approval since migration
 * 0036 -- but nothing in the browser ever listed them. Someone could raise a
 * movement and no one could see it waiting, approve it, or reject it. The
 * control was enforced; the workflow was not reachable.
 *
 * Why a cross-loan list rather than a panel on the loan page. An approver does
 * not know which loan has something pending -- that is the question the queue
 * answers. A per-loan panel can only be opened by someone who already knows.
 *
 * Authority is decided server-side on every resolve. `GET /movements` is
 * readable by any staff principal -- visibility is not authority -- so a CSR
 * sees this list and gets no buttons. Which staff may approve WHAT (the
 * thresholds, the permitted loan statuses, the self-approval refusal) is
 * servicing's decision against a principal the browser cannot forge.
 * Everything below is presentation over that.
 *
 * What this page does NOT do. It predicts two of servicing's refusals -- the
 * CSR role and self-approval -- and no others. Above the configured admin
 * threshold only an admin may approve, and this page shows an underwriter an
 * enabled Approve button for such a movement anyway; the server refuses it and
 * the refusal is displayed verbatim. That is deliberate. The threshold is
 * servicing's configuration, and a copy of it compiled into the browser is a
 * second source for one number, free to drift the moment the deployed value
 * changes -- the failure mode this repository has already had to correct in a
 * published policy. A prediction that is merely absent costs a wasted click;
 * one that is confidently wrong costs the operator their trust in the screen.
 */

interface Movement {
  id: number;
  loan_id: number;
  component: string;
  amount: number;
  entry_type: string;
  reason: string;
  requested_by: number;
  requested_role: string;
  requested_at: string | null;
  // Present only on `state=resolved` rows. Optional rather than nullable: a
  // pending movement does not have these fields at all, and modelling that as
  // "null" would invite rendering an empty resolution block above the buttons.
  resolution?: "approved" | "rejected";
  resolved_by?: number;
  resolved_role?: string;
  resolved_at?: string | null;
  // NULL on a rejection, and that is the answer rather than missing data: an
  // approval writes a ledger entry, a rejection does not.
  ledger_entry_id?: number | null;
  resolved_threshold?: number | null;
}

function ApprovalsQueue() {
  const [movements, setMovements] = useState<Movement[]>([]);
  const [history, setHistory] = useState<Movement[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  // Which loan the notice should offer to open. Set only on an approval,
  // because only an approval produced a ledger entry to go and look at.
  const [noticeLoanId, setNoticeLoanId] = useState<number | null>(null);

  // The VERIFIED principal, from `GET /auth/me` -- deliberately not `getUser()`.
  //
  // `getUser()` reads localStorage, which is mutable by anyone at the keyboard
  // and can outlive the session it describes. Every claim this page makes about
  // authority is derived from identity: which role is offered the controls, and
  // whether a row is your own. Deriving them from a cached object means the
  // screen publishes a claim its own data source cannot back -- a tampered or
  // stale `role` shows a CSR the Approve button, and a wrong `id` makes your own
  // proposal look resolvable. Neither can actually move money (servicing checks
  // the signed principal), but a control that offers an authority the caller
  // does not have is the exact failure this page exists to remove.
  //
  // `RequireRole` already fetches /auth/me to decide whether to render at all;
  // it keeps only a yes/no. This asks for the answer it discarded.
  const [me, setMe] = useState<{ id: string | number; role: string } | null>(null);

  // A CSR may read this queue and resolve nothing (specs/0002, role matrix).
  // Null until /auth/me answers, so no control is offered on a guess.
  const mayResolve = me?.role === "underwriter" || me?.role === "admin";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [who, res, done] = await Promise.all([
        apiGet("/auth/me") as Promise<{ id: string | number; role: string }>,
        apiGet("/lss/movements") as Promise<{ movements?: Movement[] }>,
        // Recent history, from the same endpoint under the same principal
        // check. Requested together with the queue so the two panels are
        // always read at one instant: fetching them in sequence lets a
        // movement be resolved in between and appear in neither, which is the
        // exact disappearance this section exists to stop.
        apiGet("/lss/movements?state=resolved") as Promise<{ movements?: Movement[] }>,
      ]);
      setMe(who);
      setMovements(res.movements ?? []);
      setHistory(done.movements ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The queue could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function resolve(id: number, resolution: "approved" | "rejected") {
    setBusyId(id);
    setNotice(null);
    setError(null);
    // Captured before the resolve: `load()` removes the row from `movements`,
    // so reading the loan id off it afterwards would find nothing to link to.
    const loanId = movements.find((m) => m.id === id)?.loan_id;
    try {
      await apiPost(`/lss/movements/${id}/resolve`, { resolution });
      setNotice(
        resolution === "approved"
          ? `Movement ${id} approved. The ledger entry is written.`
          : `Movement ${id} rejected. It stays on record; no money moved.`
      );
      setNoticeLoanId(resolution === "approved" ? loanId ?? null : null);
      await load();
    } catch (e) {
      // The server's own words. A refusal here is a real authorisation
      // decision -- self-approval, a threshold, a loan whose status changed
      // while the proposal waited -- and paraphrasing it hides which one.
      setError(e instanceof Error ? e.message : "The movement could not be resolved.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="wrap">
      <h1>Approvals</h1>
      <p className="sub">
        Money movements raised by one member of staff and waiting on a different
        one. Nothing listed here has moved any money yet.
      </p>

      {notice ? (
        <p className="alert alert-success" data-testid="approvals-notice">
          {notice}
          {noticeLoanId != null ? (
            <>
              {" "}
              <Link href={`/servicing/${noticeLoanId}#account-activity`}>
                See it in Account activity
              </Link>
              .
            </>
          ) : null}
        </p>
      ) : null}
      {error ? <p className="alert alert-error">{error}</p> : null}

      <h2 className="section-title" data-testid="approvals-pending-heading">
        Pending
      </h2>

      {/* Both lists are wrapped so a caller can say WHICH list it means. The
          same movement now legitimately appears in one and then the other, so
          "Movement 41 is on this page" stopped being a question with one
          answer -- and a spec asserting a resolved proposal had left the queue
          would otherwise read the history entry as the queue entry. */}
      <div data-testid="approvals-pending">
      {loading ? (
        <div className="card empty">Loading the queue…</div>
      ) : movements.length === 0 ? (
        <div className="card empty" data-testid="approvals-pending-empty">
          Nothing is waiting for approval.
        </div>
      ) : (
        movements.map((m) => {
          // `me` is null until /auth/me answers. Guarded explicitly rather than
          // relying on `String(undefined)` never colliding with a real id.
          const mine = me != null && String(m.requested_by) === String(me.id);
          return (
            <section className="card" key={m.id}>
              <div className="card-head">
                <span className="card-title">
                  Movement {m.id} · {m.entry_type} · {m.component}
                </span>
                <span className="num">{usd(m.amount)}</span>
              </div>

              <p>{m.reason}</p>

              <div className="spread">
                <span className="muted">
                  Raised by {mine ? "you" : `user ${m.requested_by}`} (
                  {m.requested_role})
                  {m.requested_at ? ` · ${shortDate(m.requested_at)}` : ""} ·{" "}
                  <Link href={`/servicing/${m.loan_id}`}>Loan {m.loan_id}</Link>
                </span>

                {mayResolve ? (
                  <span className="row">
                    {mine ? (
                      <span className="muted">
                        You raised this — a different approver is required.
                      </span>
                    ) : null}
                    <button
                      className="btn btn-sm"
                      disabled={mine || busyId === m.id}
                      onClick={() => resolve(m.id, "approved")}
                    >
                      Approve
                    </button>
                    <button
                      className="btn-ghost btn-sm"
                      disabled={mine || busyId === m.id}
                      onClick={() => resolve(m.id, "rejected")}
                    >
                      Reject
                    </button>
                  </span>
                ) : (
                  <span className="muted">
                    Your role can see this queue but not resolve it.
                  </span>
                )}
              </div>
            </section>
          );
        })
      )}

      </div>

      <h2 className="section-title" data-testid="approvals-resolved-heading">
        Recently resolved
      </h2>
      <p className="sub">
        What happened to proposals that have already been decided. An approval
        wrote a ledger entry; a rejection wrote none and moved no money.
      </p>

      <div data-testid="approvals-resolved">
      {loading ? (
        <div className="card empty">Loading recent decisions…</div>
      ) : history.length === 0 ? (
        <div className="card empty" data-testid="approvals-resolved-empty">
          Nothing has been resolved yet.
        </div>
      ) : (
        history.map((m) => {
          const approved = m.resolution === "approved";
          const mine = me != null && String(m.requested_by) === String(me.id);
          return (
            <section
              className="card"
              key={`resolved-${m.id}`}
              data-testid={`resolved-movement-${m.id}`}
            >
              <div className="card-head">
                <span className="card-title">
                  Movement {m.id} · {m.entry_type} · {m.component}
                </span>
                <span className="row">
                  <span className="num">{usd(m.amount)}</span>
                  <span
                    className={approved ? "badge" : "badge badge-muted"}
                    data-testid={`resolution-${m.id}`}
                  >
                    {approved ? "Approved" : "Rejected"}
                  </span>
                </span>
              </div>

              <p>{m.reason}</p>

              <div className="spread">
                <span className="muted">
                  Raised by {mine ? "you" : `user ${m.requested_by}`} (
                  {m.requested_role})
                  {m.requested_at ? ` · ${shortDate(m.requested_at)}` : ""}
                </span>
                <span className="muted">
                  {/* The second person, named. That a DIFFERENT person resolved
                      it is the whole control, so the page says who rather than
                      only that it was decided. */}
                  {m.resolution === "approved" ? "Approved" : "Rejected"} by user{" "}
                  {m.resolved_by} ({m.resolved_role})
                  {m.resolved_at ? ` · ${shortDate(m.resolved_at)}` : ""}
                </span>
              </div>

              <div className="spread">
                <span className="muted">
                  {/* The evidence, or the honest absence of it. `ledger_entry_id`
                      is the account of whether money moved -- it is written by
                      the same transaction that writes the entry -- so this is
                      read from the id itself rather than from the status word,
                      which could drift from the ledger. */}
                  {m.ledger_entry_id != null ? (
                    <>
                      Ledger entry {m.ledger_entry_id} ·{" "}
                      <Link href={`/servicing/${m.loan_id}#account-activity`}>
                        See it in Account activity
                      </Link>
                    </>
                  ) : (
                    <>No ledger entry — no money moved.</>
                  )}
                </span>
                <span className="muted">
                  {/* Recorded at resolution time, not read from configuration
                      now: a history of approvals is unreadable if the bar moved
                      and nothing says when (spec 0002 AC-22). */}
                  {m.resolved_threshold != null
                    ? `Judged against a ${usd(m.resolved_threshold)} threshold · `
                    : ""}
                  <Link href={`/servicing/${m.loan_id}`}>Loan {m.loan_id}</Link>
                </span>
              </div>
            </section>
          );
        })
      )}
      </div>
    </div>
  );
}

export default function ApprovalsPage() {
  // Staff only at the browser; the gateway enforces the same thing server-side.
  return (
    <RequireRole allow={["csr", "underwriter", "admin"]}>
      <ApprovalsQueue />
    </RequireRole>
  );
}
