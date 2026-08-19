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
}

function ApprovalsQueue() {
  const [movements, setMovements] = useState<Movement[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

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
      const [who, res] = await Promise.all([
        apiGet("/auth/me") as Promise<{ id: string | number; role: string }>,
        apiGet("/lss/movements") as Promise<{ movements?: Movement[] }>,
      ]);
      setMe(who);
      setMovements(res.movements ?? []);
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
    try {
      await apiPost(`/lss/movements/${id}/resolve`, { resolution });
      setNotice(
        resolution === "approved"
          ? `Movement ${id} approved. The ledger entry is written.`
          : `Movement ${id} rejected. It stays on record; no money moved.`
      );
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

      {notice ? <p className="alert alert-success">{notice}</p> : null}
      {error ? <p className="alert alert-error">{error}</p> : null}

      {loading ? (
        <div className="card empty">Loading the queue…</div>
      ) : movements.length === 0 ? (
        <div className="card empty">Nothing is waiting for approval.</div>
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
