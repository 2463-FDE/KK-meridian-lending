"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import LoanSummaryCard from "../../../components/LoanSummaryCard";
import RequireRole from "../../../components/RequireRole";
import StatusChip from "../../../components/StatusChip";
import { apiGet, apiPost } from "../../../lib/api";
import { usd, pct, shortDate, paymentPlanText } from "../../../lib/format";

interface Kyc {
  name_verified?: boolean;
  dob_verified?: boolean;
  address_verified?: boolean;
  ssn_verified?: boolean;
}

interface Offer {
  // The CONTRACTUAL interest rate the payments are priced at -- NOT the APR.
  // Optional: a pre-0030 offer has no stored note rate, and the summary shows
  // an em dash rather than presenting the APR as if it were the note rate.
  note_rate_pct?: number | null;
  // The federal APR: the note rate plus the prepaid origination fee, so always
  // the larger of the two once a fee exists.
  apr: number;
  finance_charge: number;
  // The REGULAR payment. Model B bills final_payment in the last period.
  monthly_payment: number;
  amount_financed: number;
  total_of_payments: number;
  // Null on a legacy offer that never recorded a schedule.
  regular_payment_count?: number | null;
  final_payment?: number | null;
  term_months?: number | null;
}

interface Applicant {
  id?: number;
  name?: string;
  email?: string;
  phone?: string;
  address?: string;
  is_entity?: boolean;
}

interface Application {
  id: string | number;
  // The detail endpoint returns `applicant` as a nested object; the list
  // endpoint returns a flat `applicant_name` string. Support both.
  applicant?: Applicant | string;
  applicant_name?: string;
  amount: number;
  term_months: number;
  purpose: string;
  status: string;
  employer?: string;
  job_title?: string;
  created_at?: string;
  kyc?: Kyc;
  decision?: string;
  // Whether this application's offer carries the full contractual schedule
  // boarding needs -- a different question from whether `offer` is present.
  offer_ready?: boolean;
  // Review fix: once staff decides, that decision is final -- this tells
  // the frontend to disable Approve/Deny up front instead of only finding
  // out via a 409 on submit.
  decision_final?: boolean;
  // Bug fix: without these, the finalized-decision panel had nothing real
  // to show -- staff could only see the original reason/who/when by
  // deliberately attempting (and being blocked by) a second decision.
  decision_reason?: string;
  decision_by?: string;
  decision_at?: string;
  offer?: Offer;
}

interface DecisionResult {
  app_id: string | number;
  decision: string;
  score?: number;
  adverse_action_reason?: string;
}

const OFFER_RATE_PCT = 7.99;

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

function prettyPurpose(p?: string): string {
  return (p || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function UnderwritingDetailPage() {
  return (
    <RequireRole allow={["underwriter", "admin", "csr"]}>
      <UnderwritingDetailContent />
    </RequireRole>
  );
}

function UnderwritingDetailContent() {
  const params = useParams<{ appId: string }>();
  const appId = params?.appId;

  const [app, setApp] = useState<Application | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // action state (mirrors the servicing detail action pattern)
  const [decision, setDecision] = useState<DecisionResult | null>(null);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [offerReady, setOfferReady] = useState(false);
  const [boardedLoanId, setBoardedLoanId] = useState<string | number | null>(
    null
  );
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  // Manual review (feature: staff tool to resolve a "refer" decision) --
  // see app/routers/applications.py::review_application.
  const [reviewOutcome, setReviewOutcome] = useState<"approve" | "deny">("approve");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewErr, setReviewErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!appId) return;
    setLoading(true);
    setError(null);
    try {
      const a = (await apiGet(`/los/applications/${appId}`)) as Application;
      setApp(a);
      if (a.offer) setOffer(a.offer);
      // Boardability is reported separately from the disclosure: an offer
      // predating the stored Model B schedule still displays its disclosed
      // amounts but cannot be funded from them.
      setOfferReady(Boolean(a.offer_ready));
    } catch (err) {
      setError(errMsg(err, "Could not load this application."));
      setApp(null);
    } finally {
      setLoading(false);
    }
  }, [appId]);

  useEffect(() => {
    load();
  }, [load]);

  async function runDecision() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(
        `/los/applications/${appId}/decision`
      )) as DecisionResult;
      setDecision(res);
      setApp((prev) => (prev ? { ...prev, decision: res.decision } : prev));
      setActionMsg(`Decision recorded: ${res.decision}.`);
    } catch (err) {
      setActionErr(errMsg(err, "Could not run a decision."));
    } finally {
      setActionBusy(false);
    }
  }

  async function submitReview() {
    if (!appId || !reviewReason.trim()) return;
    setReviewBusy(true);
    setReviewErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(`/los/applications/${appId}/review`, {
        outcome: reviewOutcome,
        reason: reviewReason.trim(),
      })) as DecisionResult;
      setDecision(res);
      setReviewReason("");
      setActionMsg(`Decision recorded: ${res.decision}. This decision is final.`);
      // Bug fix: an approve can auto-generate an offer server-side (same as
      // the automated approve path), but this response only carries the
      // decision outcome, not the offer itself -- reload the application so
      // `offer` (and status) reflect what the server actually did instead of
      // going stale until the next manual page refresh.
      await load();
    } catch (err) {
      setReviewErr(errMsg(err, "Could not record this review."));
    } finally {
      setReviewBusy(false);
    }
  }

  async function makeOffer() {
    if (!app || !appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost("/los/offer", {
        app_id: appId,
        principal: app.amount,
        annual_rate_pct: OFFER_RATE_PCT,
        term_months: app.term_months,
      })) as { app_id: string | number; disclosure?: Offer; offer?: Offer };
      const disc = res.disclosure ?? res.offer ?? null;
      setOffer(disc);
      setActionMsg("Offer generated.");
      // Re-read rather than assuming the new offer is boardable. Whether the
      // full contractual schedule was persisted is a server-side fact, and
      // /los/offer's response body does not report it -- inferring boardability
      // from "an offer came back" is exactly the conflation offer_ready exists
      // to remove.
      await load();
    } catch (err) {
      setActionErr(errMsg(err, "Could not generate an offer."));
    } finally {
      setActionBusy(false);
    }
  }

  async function acceptAndBoard() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(`/los/applications/${appId}/accept`)) as {
        loan_id: string | number;
      };
      setBoardedLoanId(res.loan_id);
      setActionMsg(`Boarded to servicing as loan #${String(res.loan_id)}.`);
    } catch (err) {
      setActionErr(errMsg(err, "Could not accept and board this application."));
    } finally {
      setActionBusy(false);
    }
  }

  if (loading && !app) {
    return (
      <main className="wrap">
        <p className="muted">Loading application #{appId}…</p>
      </main>
    );
  }

  if (error && !app) {
    return (
      <main className="wrap">
        <p>
          <Link href="/underwriting">← Back to underwriting</Link>
        </p>
        <div className="alert alert-error">{error}</div>
      </main>
    );
  }

  const applicantObj =
    app && typeof app.applicant === "object" ? app.applicant : null;
  const applicantName =
    applicantObj?.name ||
    app?.applicant_name ||
    (typeof app?.applicant === "string" ? app.applicant : "") ||
    "Applicant";
  const currentDecision = decision?.decision || app?.decision || null;

  return (
    <main className="wrap">
      <p style={{ marginBottom: 12 }}>
        <Link href="/underwriting">← Back to underwriting</Link>
      </p>

      {/* Header */}
      <div className="spread">
        <div>
          <h1 style={{ marginBottom: 6 }}>{applicantName}</h1>
          <p className="sub" style={{ margin: 0 }}>
            Application #{String(appId)}
          </p>
        </div>
        {app ? <StatusChip status={app.status} /> : null}
      </div>

      {/* Request summary */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Requested amount</div>
          <div className="kpi-value">{usd(app?.amount)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Term</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {app?.term_months} months
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Received</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {shortDate(app?.created_at)}
          </div>
        </div>
      </div>

      {/* Applicant detail */}
      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>
          Applicant
        </div>
        <div className="dl">
          <div className="dl-row">
            <dt>Name</dt>
            <dd>{applicantName}</dd>
          </div>
          <div className="dl-row">
            <dt>Type</dt>
            <dd>{applicantObj?.is_entity ? "Entity / business" : "Individual"}</dd>
          </div>
          <div className="dl-row">
            <dt>Email</dt>
            <dd>{applicantObj?.email || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Phone</dt>
            <dd>{applicantObj?.phone || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Address</dt>
            <dd>{applicantObj?.address || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Purpose</dt>
            <dd>{prettyPurpose(app?.purpose)}</dd>
          </div>
          <div className="dl-row">
            <dt>Employer</dt>
            <dd>{app?.employer || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Job title</dt>
            <dd>{app?.job_title || "—"}</dd>
          </div>
        </div>
      </div>

      {/* KYC */}
      <h2>Identity verification (KYC)</h2>
      <div className="card">
        <div className="dl">
          <KycRow label="Name" ok={app?.kyc?.name_verified} />
          <KycRow label="Date of birth" ok={app?.kyc?.dob_verified} />
          <KycRow label="Address" ok={app?.kyc?.address_verified} />
          <KycRow label="SSN" ok={app?.kyc?.ssn_verified} />
        </div>
      </div>

      {/* AI Summary */}
      <h2>AI Summary</h2>
      {appId ? <LoanSummaryCard appId={appId} /> : null}

      {/* Action feedback (shared by all panels) */}
      {actionMsg ? <div className="alert alert-success">{actionMsg}</div> : null}
      {actionErr ? <div className="alert alert-error">{actionErr}</div> : null}

      {/* Decision */}
      <h2>Decision</h2>
      <div className="card">
        <div className="spread">
          <div>
            <div className="card-title" style={{ marginBottom: 8 }}>
              Underwriting decision
            </div>
            {currentDecision ? (
              <StatusChip status={currentDecision} />
            ) : (
              <span className="muted">No decision yet.</span>
            )}
            {typeof decision?.score === "number" ? (
              <p className="hint" style={{ marginTop: 10 }}>
                Model score: {decision.score}
              </p>
            ) : null}
            {decision?.adverse_action_reason ? (
              <div className="alert alert-warn">
                <strong>Adverse action reason:</strong>{" "}
                {decision.adverse_action_reason}
              </div>
            ) : null}
          </div>
          {/* Bug fix: this used to only disable/hide the button once funded --
              a manually-decided-but-not-yet-funded application still showed
              an enabled button that always 409'd. Disabled (with a tooltip,
              not just hidden) for either final state now, so the control
              itself reflects reality instead of only the backend's error. */}
          <button
            onClick={runDecision}
            disabled={actionBusy || app?.decision_final || app?.status === "funded"}
            title={
              app?.decision_final
                ? "A staff member has already made a final decision -- the automated model cannot rerun and overwrite it."
                : app?.status === "funded"
                  ? "This application is funded -- its decision can no longer be rerun."
                  : undefined
            }
          >
            {actionBusy ? "Working…" : "Run decision"}
          </button>
        </div>
        {app?.status === "funded" ? (
          <p className="hint" style={{ marginTop: 10 }}>
            {/* Bug fix: rerunning after funding used to silently reset the
                recorded decision back to the automated outcome while the
                loan sat funded on top of it -- the backend rejects this now
                (422), so the button is disabled rather than offering an
                action that always fails. */}
            This application is funded — its decision can no longer be rerun.
          </p>
        ) : app?.decision_final ? (
          <p className="hint" style={{ marginTop: 10 }}>
            A staff member has already made a final decision — the automated model cannot rerun and overwrite it.
          </p>
        ) : null}
      </div>

      {/* Manual review -- feature: staff tool to resolve a "refer" decision
          (policies/underwriting_guidelines.md's manual-review band, score
          600-659 or DTI 43-50%). Scoped to refer only -- staff cannot use
          this to override a clean automated approve/deny (the backend
          enforces this too; see review_application's "only a 'refer'
          decision can be reviewed" 422).
          The locked "finalized" info view shows whenever decision_final is
          true (a resolved refer), funded or not -- who/when/why is a
          permanent audit fact, not something that should disappear once
          the loan is boarded. */}
      {app?.decision_final || currentDecision === "refer" ? (
        <div className="card" style={{ marginTop: 16 }}>
          {app?.decision_final ? (
            // Bug fix: this used to still render the editable form (disabled)
            // bound to reviewOutcome's own default local state -- so a
            // finalized DENY could visibly show "Approve" pre-selected,
            // directly contradicting the decision chip right above it. Once
            // final, show the ACTUAL recorded decision, not editable state
            // that was never meant to reflect it.
            <>
              <div className="card-title" style={{ marginBottom: 8 }}>
                Decision finalized
              </div>
              <p className="hint" style={{ marginBottom: 16 }}>
                Staff has already made a final decision on this application. It cannot be changed.
              </p>
              <label>Decision</label>
              <div style={{ marginBottom: 12 }}>
                {currentDecision === "approve" ? "Approve" : "Deny"}
              </div>
              <label>Reason</label>
              <div className="hint" style={{ marginBottom: 12 }}>
                {app?.decision_reason || "—"}
              </div>
              {app?.decision_by ? (
                <p className="hint">
                  Decided by {app.decision_by}
                  {app?.decision_at ? ` on ${shortDate(app.decision_at)}` : ""}.
                </p>
              ) : null}
            </>
          ) : (
            <>
              <div className="card-title" style={{ marginBottom: 8 }}>
                Manual review required
              </div>
              <p className="hint" style={{ marginBottom: 16 }}>
                This application scored in the manual-review band. Resolve it below — the applicant can’t accept an offer until this is decided.
              </p>
              <label>Decision</label>
              <select
                value={reviewOutcome}
                onChange={(e) => setReviewOutcome(e.target.value as "approve" | "deny")}
              >
                <option value="approve">Approve</option>
                <option value="deny">Deny</option>
              </select>
              <label>Reason (shown to the applicant if denied)</label>
              <textarea
                rows={3}
                value={reviewReason}
                onChange={(e) => setReviewReason(e.target.value)}
                placeholder="e.g. DTI recalculated under policy threshold after verifying updated income"
              />
              {reviewErr ? <div className="alert alert-error">{reviewErr}</div> : null}
              <button
                style={{ marginTop: 14 }}
                onClick={submitReview}
                disabled={reviewBusy || !reviewReason.trim()}
              >
                {reviewBusy ? "Recording…" : `Record ${reviewOutcome === "approve" ? "approval" : "denial"}`}
              </button>
            </>
          )}
        </div>
      ) : null}

      {/* Offer */}
      <h2>Offer</h2>
      <div className="card">
        <div className="spread" style={{ marginBottom: offer ? 16 : 0 }}>
          <p className="hint hint-strong" style={{ margin: 0 }}>
            Generate a Truth-in-Lending offer using a {pct(OFFER_RATE_PCT)} note
            rate for {usd(app?.amount)} over {app?.term_months} months.
          </p>
          {offer && offerReady ? (
            // Not a control. "Offer already created" describes state and can
            // never be actioned -- rendering it as a disabled button invited
            // clicks on something that was never going to respond, and read to
            // a screen reader as an unavailable action rather than a fact.
            // A status message says the same thing honestly.
            <span className="status-note" role="status" data-testid="offer-exists">
              Offer already created
            </span>
          ) : offer && !offerReady ? (
            // A legacy offer: its five TILA amounts are there, but not the
            // stored schedule boarding needs, so Accept below is disabled and
            // its tooltip tells staff to regenerate. Until now there was
            // nothing to click -- this branch showed the same static "Offer
            // already created" label, so the audited repair path added for
            // exactly these rows had no caller in any production UI. Review
            // finding on PR #10.
            <button
              className="btn-ghost"
              onClick={makeOffer}
              disabled={actionBusy || currentDecision !== "approve"}
              data-testid="regenerate-offer"
              title={
                "This offer predates the stored payment schedule, so it cannot "
                + "be boarded. Regenerating writes a new disclosure at today's "
                + "terms and records the change in the audit log."
              }
            >
              {actionBusy ? "Regenerating…" : "Regenerate offer"}
            </button>
          ) : (
            <button
              className="btn-ghost"
              onClick={makeOffer}
              disabled={actionBusy || currentDecision !== "approve"}
              // aria-disabled alongside `disabled` so assistive technology is
              // told why the control is unavailable rather than skipping it
              // silently; the title carries the reason for pointer users.
              aria-disabled={actionBusy || currentDecision !== "approve"}
              title={
                currentDecision === "deny"
                  ? `An offer cannot be created because this application was denied.${decision?.adverse_action_reason ? ` Decision reason: ${decision.adverse_action_reason}` : ""}`
                  : currentDecision !== "approve"
                    ? "An offer cannot be created until the application receives a final approval."
                    : undefined
              }
            >
              {actionBusy ? "Working…" : "Make offer"}
            </button>
          )}
        </div>

        {offer ? (
          <>
          {/* Both rates, before the federal box. They are different numbers --
              the note rate prices the payments, the APR adds the prepaid
              origination fee -- and showing one alone is what let a 5.43%
              "APR" sit under a 7.99% loan without looking wrong. */}
          <div className="rate-summary" data-testid="rate-summary">
            <div className="rate-summary-item">
              <span className="rate-summary-label">Interest rate (note rate)</span>
              <span className="rate-summary-value" data-testid="note-rate">
                {offer.note_rate_pct != null ? pct(offer.note_rate_pct) : "—"}
              </span>
            </div>
            <div className="rate-summary-item">
              <span className="rate-summary-label">Federal APR</span>
              <span className="rate-summary-value" data-testid="federal-apr">
                {pct(offer.apr)}
              </span>
            </div>
          </div>
          <div className="tila">
            <div className="tila-title">Federal Truth-in-Lending Disclosure</div>
            <div className="tila-grid">
              <div className="tila-cell tila-cell-apr">
                <div className="tila-cell-label">Annual Percentage Rate</div>
                <div className="tila-cell-desc">
                  The total cost of your credit as a yearly rate, including the
                  origination fee.
                </div>
                <div className="tila-cell-value">{pct(offer.apr)}</div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Finance Charge</div>
                <div className="tila-cell-desc">
                  The dollar amount the credit will cost.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.finance_charge)}
                </div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Amount Financed</div>
                <div className="tila-cell-desc">
                  The amount of credit provided.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.amount_financed)}
                </div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Total of Payments</div>
                <div className="tila-cell-desc">
                  What will be paid after all payments are made.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.total_of_payments)}
                </div>
              </div>
            </div>
            {/* Payment schedule: a full-width row INSIDE the box, beneath the
                four federal cells. The four boxes are the federal disclosure
                and do not carry the schedule, so staff had no way to see that
                the final payment differs from the regular one -- which under
                Model B it almost always does.

                Previously a bare <p> appended here, which .tila's
                overflow:hidden and zero padding pushed against the border. Now
                a real row with real padding, so nothing can sit on the border
                at any viewport width. */}
            <div className="tila-schedule" data-testid="payment-schedule">
              <div className="tila-schedule-label">Payment schedule</div>
              <div className="tila-schedule-value">
                {offer.regular_payment_count != null && offer.final_payment != null ? (
                  paymentPlanText(
                    offer.monthly_payment,
                    offer.regular_payment_count,
                    offer.final_payment,
                  )
                ) : (
                  <>
                    Monthly payment {usd(offer.monthly_payment)}. No contractual
                    payment schedule was recorded for this offer, so the final
                    payment is not known and it cannot be boarded.
                  </>
                )}
              </div>
            </div>
          </div>
          </>
        ) : null}
      </div>

      {/* Accept & board */}
      <h2>Board to servicing</h2>
      <div className="card">
        {boardedLoanId ? (
          <div className="alert alert-success" style={{ margin: 0 }}>
            Boarded. Loan <strong>#{String(boardedLoanId)}</strong> created.{" "}
            <Link href={`/servicing/${boardedLoanId}`}>
              Open the loan account →
            </Link>
          </div>
        ) : app?.status === "funded" ? (
          // Bug fix: boardedLoanId is session-local -- reloading the page
          // after an earlier session already boarded this application left
          // the button enabled again, as if nothing had happened. app.status
          // is the persisted source of truth.
          <div className="alert alert-success" style={{ margin: 0 }}>
            This application has already been boarded.
          </div>
        ) : (
          <div className="spread">
            <p className="hint" style={{ margin: 0 }}>
              Accept the offer and board this application as a serviced loan.
            </p>
            <button
              onClick={acceptAndBoard}
              disabled={actionBusy || currentDecision !== "approve" || !offerReady}
              title={
                currentDecision === "deny"
                  ? `This application cannot be boarded because it was denied.${decision?.adverse_action_reason ? ` Reason: ${decision.adverse_action_reason}` : ""}`
                  : currentDecision !== "approve"
                    ? "This application must receive final approval before it can be boarded."
                    : !offer
                      ? "Create an offer before boarding this application."
                      : !offerReady
                        ? "This offer predates the stored payment schedule, so it cannot be boarded. Regenerate the offer to record the schedule."
                        : undefined
              }
            >
              {actionBusy ? "Working…" : "Accept & board"}
            </button>
          </div>
        )}
      </div>
    </main>
  );
}

function KycRow({ label, ok }: { label: string; ok?: boolean }) {
  return (
    <div className="dl-row">
      <dt>{label}</dt>
      <dd>
        {ok ? (
          <span className="chip chip-green">Verified</span>
        ) : (
          <span className="chip chip-amber">Unverified</span>
        )}
      </dd>
    </div>
  );
}
