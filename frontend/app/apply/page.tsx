"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import Stepper, { type Step } from "../../components/Stepper";
import StatusChip from "../../components/StatusChip";
import { apiGet, apiPost, ApiError } from "../../lib/api";
import { usd, pct } from "../../lib/format";

const STEPS: Step[] = [
  { n: 1, label: "Personal" },
  { n: 2, label: "Employment & Income" },
  { n: 3, label: "Loan Details" },
  { n: 4, label: "Review" },
  { n: 5, label: "Decision & Offer" },
];

const PURPOSES = [
  { value: "debt_consolidation", label: "Debt consolidation" },
  { value: "home_improvement", label: "Home improvement" },
  { value: "auto", label: "Auto" },
  { value: "medical", label: "Medical" },
  { value: "personal", label: "Personal" },
  { value: "other", label: "Other" },
];

// Backend (origination-service ApplicationIn) has no separate city/state
// columns -- just one free-text `address` string plus `zip_code`. Street/
// City/State are a UI-only split for a proper address form; submitApplication
// joins them back into that one string before the API call.
const US_STATES = [
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
  "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
  "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
  "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
  "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
];

const OFFER_RATE_PCT = 7.99;
const MIN_AGE_YEARS = 18; // lending policy floor -- see policy-chat's eligibility excerpt

function isoDateYearsAgo(years: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - years);
  return d.toISOString().slice(0, 10);
}

// Bounds the native date picker itself so a future date or a date implying
// under-18/over-120 was never selectable in the first place, not just
// rejected after the fact.
const MAX_DOB = isoDateYearsAgo(MIN_AGE_YEARS);
const MIN_DOB = isoDateYearsAgo(120);

interface FormState {
  name: string;
  dob: string;
  ssn: string;
  email: string;
  phone: string;
  street: string;
  city: string;
  state: string;
  zip_code: string;
  employer: string;
  job_title: string;
  annual_income: string;
  employment_years: string;
  amount: number;
  term_months: string;
  purpose: string;
}

interface Kyc {
  name_verified?: boolean;
  dob_verified?: boolean;
  address_verified?: boolean;
  ssn_verified?: boolean;
}

interface AppResult {
  app_id: string | number;
  status?: string;
  kyc?: Kyc;
  // Review fix: anonymous applicants have no session -- this proves ownership
  // on the first /decision call (see DecisionIn.access_token,
  // origination-service's run_decision). Dropping it here silently 403'd every
  // real borrower's own decision request.
  access_token?: string;
}

interface DecisionResult {
  app_id: string | number;
  decision: string;
  score?: number;
  adverse_action_reason?: string;
  // Review fix: one-time proof of ownership, minted only when approved -- the
  // no-account borrower flow has no session, so this stands in for one when
  // accepting (see acceptOffer below and origination-service's accept_offer).
  accept_token?: string;
}

interface Disclosure {
  apr: number;
  finance_charge: number;
  monthly_payment: number;
  amount_financed: number;
  total_of_payments: number;
  schedule?: {
    n: number;
    due_date: string;
    payment: number;
    principal: number;
    interest: number;
    balance: number;
  }[];
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

function maskSsn(ssn: string): string {
  const digits = ssn.replace(/\D/g, "");
  if (digits.length < 4) return "•••-••-••••";
  return `•••-••-${digits.slice(-4)}`;
}

// Live-format as the borrower types -- backend normalizes by stripping
// non-digits itself (origination-service's ApplicationIn validators), so a
// dashed/parenthesized display value is safe to submit as-is.
function formatSsnInput(raw: string): string {
  const d = raw.replace(/\D/g, "").slice(0, 9);
  if (d.length <= 3) return d;
  if (d.length <= 5) return `${d.slice(0, 3)}-${d.slice(3)}`;
  return `${d.slice(0, 3)}-${d.slice(3, 5)}-${d.slice(5)}`;
}
function formatPhoneInput(raw: string): string {
  const d = raw.replace(/\D/g, "").slice(0, 10);
  if (d.length === 0) return "";
  if (d.length < 4) return `(${d}`;
  if (d.length < 7) return `(${d.slice(0, 3)}) ${d.slice(3)}`;
  return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
}

export default function ApplyPage() {
  const [step, setStep] = useState(1);
  // Review-step edit affordance. The trainer could not correct anything from
  // the review screen: Back existed and preserved answers, but reaching a
  // Step 1 field meant three Back clicks and then three Next clicks to get
  // home again, so in practice nobody used it. Each summary group now has its
  // own Edit control; this flag is what lets the edited step offer a direct
  // way back instead of making the user walk the wizard forward again.
  const [returningToReview, setReturningToReview] = useState(false);
  // Focus target for the edit round-trip. Activating Edit unmounts the button
  // that had focus, which drops focus to <body> -- a keyboard or screen-reader
  // user is then given no indication that the page changed under them. The
  // heading of the step we jumped to is the announcement point, so focus moves
  // there and the step's name is read out.
  const stepHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const [focusStepHeading, setFocusStepHeading] = useState(false);
  useEffect(() => {
    if (focusStepHeading && stepHeadingRef.current) {
      stepHeadingRef.current.focus();
      setFocusStepHeading(false);
    }
  }, [focusStepHeading, step]);
  const [form, setForm] = useState<FormState>({
    name: "",
    dob: "",
    ssn: "",
    email: "",
    phone: "",
    street: "",
    city: "",
    state: "",
    zip_code: "",
    employer: "",
    job_title: "",
    annual_income: "",
    employment_years: "",
    amount: 15000,
    term_months: "36",
    purpose: "debt_consolidation",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [showSsn, setShowSsn] = useState(false);

  // submission / decision / offer state
  const [busy, setBusy] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);
  const [app, setApp] = useState<AppResult | null>(null);
  const [decision, setDecision] = useState<DecisionResult | null>(null);
  const [disclosure, setDisclosure] = useState<Disclosure | null>(null);
  const [acceptedLoanId, setAcceptedLoanId] = useState<string | number | null>(
    null
  );
  const [showSchedule, setShowSchedule] = useState(false);

  function set<K extends keyof FormState>(k: K, v: FormState[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  function validateStep(s: number): boolean {
    const e: Record<string, string> = {};
    if (s === 1) {
      if (!form.name.trim()) e.name = "Required";
      if (!form.dob) e.dob = "Required";
      else if (Number.isNaN(new Date(form.dob).getTime())) e.dob = "Enter a valid date";
      else if (form.dob > MAX_DOB) e.dob = `Must be at least ${MIN_AGE_YEARS} years old`;
      else if (form.dob < MIN_DOB) e.dob = "Enter a valid date of birth";
      if (!form.ssn.trim()) e.ssn = "Required";
      else if (form.ssn.replace(/\D/g, "").length !== 9)
        e.ssn = "Enter a 9-digit SSN";
      if (!form.email.trim()) e.email = "Required";
      else if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.email))
        e.email = "Enter a valid email";
      if (!form.phone.trim()) e.phone = "Required";
      else if (form.phone.replace(/\D/g, "").length !== 10)
        e.phone = "Enter a 10-digit phone number";
      if (!form.street.trim()) e.street = "Required";
      if (!form.city.trim()) e.city = "Required";
      if (!form.state.trim()) e.state = "Required";
      if (!form.zip_code.trim()) e.zip_code = "Required";
      else if (form.zip_code.replace(/\D/g, "").length !== 5 && form.zip_code.replace(/\D/g, "").length !== 9)
        e.zip_code = "Enter a 5-digit ZIP code";
    } else if (s === 2) {
      if (!form.employer.trim()) e.employer = "Required";
      if (!form.job_title.trim()) e.job_title = "Required";
      if (!form.annual_income.trim()) e.annual_income = "Required";
      else if (Number(form.annual_income) <= 0)
        e.annual_income = "Must be greater than 0";
      if (!form.employment_years.trim()) e.employment_years = "Required";
      else if (Number(form.employment_years) < 0)
        e.employment_years = "Cannot be negative";
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  }

  function next() {
    if (validateStep(step)) {
      setErrors({});
      // Reaching the review the normal way ends the edit round-trip.
      if (step + 1 >= 4) setReturningToReview(false);
      setStep((s) => Math.min(5, s + 1));
    }
  }
  function back() {
    // Back walks one step backwards, and during an edit round-trip it keeps
    // `returningToReview` set on purpose: someone who jumped to step 3 and then
    // realises step 1 also needs a correction can Back to it and still get home
    // in one click. It is not a cancel -- edits are already in form state (see
    // the "Return to review" button) -- and it cannot smuggle an invalid value
    // into the review, because returnToReview() re-validates steps 1-3.
    setErrors({});
    setStep((s) => Math.max(1, s - 1));
  }

  /** Jump straight from the review to the step that owns a field. */
  function editStep(target: number) {
    setErrors({});
    setReturningToReview(true);
    setStep(target);
    setFocusStepHeading(true);
  }

  /** Return to the review, but only if what was just edited is still valid --
   * otherwise an edit could put the application back into review carrying a
   * value the wizard would never have accepted going forward. */
  function returnToReview() {
    // Validate every step that feeds the review, not just the one on screen.
    // Validating only the current step left a hole: edit step 3, type something
    // invalid, press Back to step 2, then return -- step 2 validates clean and
    // the invalid step-3 value reaches the review. Jump to the first offending
    // step instead, with its errors showing.
    for (const s of [1, 2, 3]) {
      if (!validateStep(s)) {
        if (s !== step) {
          setStep(s);
          setFocusStepHeading(true);
        }
        return;
      }
    }
    setErrors({});
    setReturningToReview(false);
    setStep(4);
  }

  async function submitApplication() {
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost("/los/applications", {
        name: form.name,
        dob: form.dob,
        ssn: form.ssn,
        address: `${form.street}, ${form.city}, ${form.state}`,
        zip_code: form.zip_code,
        email: form.email,
        phone: form.phone,
        employer: form.employer,
        job_title: form.job_title,
        income: parseFloat(form.annual_income || "0"),
        employment_years: parseInt(form.employment_years || "0", 10),
        amount: form.amount,
        term_months: parseInt(form.term_months, 10),
        purpose: form.purpose,
      })) as AppResult;
      setApp(res);
      setStep(5);
    } catch (err) {
      setApiError(errMsg(err, "Could not submit your application."));
    } finally {
      setBusy(false);
    }
  }

  async function getDecision() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost(`/los/applications/${app.app_id}/decision`, {
        access_token: app.access_token,
      })) as DecisionResult;
      setDecision(res);
    } catch (err) {
      setApiError(errMsg(err, "Could not retrieve a decision."));
    } finally {
      setBusy(false);
    }
  }

  async function viewOffer() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      // Bug fix: run_decision auto-generates an offer server-side the
      // instant a decision comes back approve (best-effort) -- this used
      // to always try to CREATE one instead, which always found that
      // auto-generated offer already there and always failed. Request the
      // existing offer first (the borrower's own accept_token, already in
      // hand from the decision response, proves ownership); only fall back
      // to creating one on a genuine 404 (auto-generation hasn't landed
      // yet, or predates it) -- and even that create is itself idempotent
      // now (returns the same offer if one shows up first in a race),
      // never a 409.
      //
      // Security fix: the token travels only as the X-Offer-Accept-Token
      // header, never a URL query parameter -- a query parameter leaks into
      // gateway/origination-service access logs, browser history, and a
      // Referer header; a header does not.
      const token = decision?.accept_token || "";
      let existing: { disclosure: Disclosure } | null = null;
      try {
        existing = (await apiGet(
          `/los/applications/${app.app_id}/offer`,
          { "X-Offer-Accept-Token": token },
        )) as { disclosure: Disclosure };
      } catch (getErr) {
        if (!(getErr instanceof ApiError) || getErr.status !== 404) throw getErr;
        // 404 -- genuinely no offer yet; fall through to create one below.
      }
      if (existing) {
        setDisclosure(existing.disclosure);
        return;
      }
      // Security fix (PR #6 review): POST /offer now requires the same
      // ownership proof as the GET above (staff or a matching
      // X-Offer-Accept-Token) -- send the same token here too, or a
      // legitimate borrower's own first-offer creation would 403.
      const created = (await apiPost(
        "/los/offer",
        {
          app_id: app.app_id,
          principal: form.amount,
          annual_rate_pct: OFFER_RATE_PCT,
          term_months: parseInt(form.term_months, 10),
        },
        { "X-Offer-Accept-Token": token },
      )) as { app_id: string | number; disclosure: Disclosure };
      setDisclosure(created.disclosure);
    } catch (err) {
      setApiError(errMsg(err, "Could not generate your offer."));
    } finally {
      setBusy(false);
    }
  }

  async function acceptOffer() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      // Security fix: same header-only transport as viewOffer above -- the
      // token used to also be accepted as a JSON body field; the body
      // itself never leaked into a log, but a single consistent transport
      // for this credential (never a query string, never re-introduced by
      // accident on this route) is the actual requirement, not "this one
      // spot happened to be safe."
      const res = (await apiPost(
        `/los/applications/${app.app_id}/accept`,
        undefined,
        { "X-Offer-Accept-Token": decision?.accept_token || "" },
      )) as { loan_id: string | number };
      setAcceptedLoanId(res.loan_id);
    } catch (err) {
      setApiError(errMsg(err, "Could not accept the offer."));
    } finally {
      setBusy(false);
    }
  }

  // Bug fix: backend always returns "approve" (decision-service/app/decision.py,
  // origination-service's own outcome == "approve" check) -- never "approved".
  // This compared against "approved" and so was always false, meaning "View
  // your offer" never appeared through this page even on a genuine approval.
  const decisionApproved = (decision?.decision || "").toLowerCase() === "approve";
  const showAside = step < 5;

  return (
    <main className="wrap">
      <p className="eyebrow">Personal Loan Application</p>
      <h1>Apply for a personal loan</h1>
      <p className="sub" style={{ marginBottom: 14 }}>
        Get a decision in minutes — no obligation until you accept your offer.
      </p>
      <div className="badge-row" style={{ marginBottom: 28 }}>
        <span className="badge">$1,000–$50,000</span>
        <span className="badge">12–60 months</span>
        <span className="badge">Fixed rate</span>
      </div>

      <Stepper steps={STEPS} current={step} />

      <div className={showAside ? "apply-grid" : undefined}>
        <div className="card apply-card">
        {/* ---- Step 1: Personal --------------------------------------- */}
        {step === 1 && (
          <>
            <StepHeader
              headingRef={stepHeadingRef}
              eyebrow="Step 1 of 5"
              title="Personal information"
              desc="This is used to verify your identity and won't affect your credit."
            />

            <div className="field-group-title">Identity</div>
            <Field label="Full name" error={errors.name}>
              <input
                autoComplete="name"
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="Jane Q. Borrower"
              />
            </Field>
            <div className="field-row">
              <Field label="Date of birth" error={errors.dob}>
                <input
                  type="date"
                  autoComplete="bday"
                  value={form.dob}
                  onChange={(e) => set("dob", e.target.value)}
                  min={MIN_DOB}
                  max={MAX_DOB}
                />
              </Field>
              <Field label="Social Security Number" error={errors.ssn}>
                <div className="input-adorn">
                  <input
                    type={showSsn ? "text" : "password"}
                    autoComplete="off"
                    inputMode="numeric"
                    value={form.ssn}
                    onChange={(e) => set("ssn", formatSsnInput(e.target.value))}
                    placeholder="123-45-6789"
                  />
                  <button
                    type="button"
                    className="input-adorn-btn"
                    onClick={() => setShowSsn((v) => !v)}
                    aria-label={showSsn ? "Hide SSN" : "Show SSN"}
                  >
                    {showSsn ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
                <p className="field-note">
                  <LockIcon />
                  Encrypted — used only to verify your identity.
                </p>
              </Field>
            </div>

            <div className="field-group-title">Contact information</div>
            <div className="field-row">
              <Field label="Email" error={errors.email}>
                <input
                  type="email"
                  autoComplete="email"
                  value={form.email}
                  onChange={(e) => set("email", e.target.value)}
                  placeholder="you@example.com"
                />
              </Field>
              <Field label="Phone" error={errors.phone}>
                <input
                  type="tel"
                  autoComplete="tel"
                  inputMode="tel"
                  value={form.phone}
                  onChange={(e) => set("phone", formatPhoneInput(e.target.value))}
                  placeholder="(555) 555-0123"
                />
              </Field>
            </div>
            <Field label="Street address" error={errors.street}>
              <div className="input-icon-left">
                <PinIcon />
                <input
                  autoComplete="address-line1"
                  value={form.street}
                  onChange={(e) => set("street", e.target.value)}
                  placeholder="123 Main St"
                />
              </div>
            </Field>
            <div className="field-row-address">
              <Field label="City" error={errors.city}>
                <input
                  autoComplete="address-level2"
                  value={form.city}
                  onChange={(e) => set("city", e.target.value)}
                  placeholder="Springfield"
                />
              </Field>
              <Field label="State" error={errors.state}>
                <select
                  autoComplete="address-level1"
                  value={form.state}
                  onChange={(e) => set("state", e.target.value)}
                >
                  <option value="">Select</option>
                  {US_STATES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="ZIP code" error={errors.zip_code}>
                <input
                  autoComplete="postal-code"
                  inputMode="numeric"
                  value={form.zip_code}
                  onChange={(e) => set("zip_code", e.target.value)}
                  placeholder="62704"
                  maxLength={10}
                />
              </Field>
            </div>
          </>
        )}

        {/* ---- Step 2: Employment & Income ---------------------------- */}
        {step === 2 && (
          <>
            <StepHeader
              headingRef={stepHeadingRef}
              eyebrow="Step 2 of 5"
              title="Employment & income"
              desc="Helps us confirm you can comfortably afford this loan."
            />
            <div className="field-row">
              <Field label="Employer" error={errors.employer}>
                <input
                  value={form.employer}
                  onChange={(e) => set("employer", e.target.value)}
                />
              </Field>
              <Field label="Job title" error={errors.job_title}>
                <input
                  value={form.job_title}
                  onChange={(e) => set("job_title", e.target.value)}
                />
              </Field>
            </div>
            <div className="field-row">
              <Field label="Annual income (USD)" error={errors.annual_income}>
                <input
                  type="number"
                  min="0"
                  value={form.annual_income}
                  onChange={(e) => set("annual_income", e.target.value)}
                  placeholder="65000"
                />
              </Field>
              <Field
                label="Years at employer"
                error={errors.employment_years}
              >
                <input
                  type="number"
                  min="0"
                  step="0.5"
                  value={form.employment_years}
                  onChange={(e) => set("employment_years", e.target.value)}
                  placeholder="3"
                />
              </Field>
            </div>
          </>
        )}

        {/* ---- Step 3: Loan Details ----------------------------------- */}
        {step === 3 && (
          <>
            <StepHeader
              headingRef={stepHeadingRef}
              eyebrow="Step 3 of 5"
              title="Loan details"
              desc="Choose the amount and term that fits your budget."
            />
            <label htmlFor="amount">Loan amount</label>
            <div className="range-readout">{usd(form.amount)}</div>
            <input
              id="amount"
              type="range"
              min={1000}
              max={50000}
              step={500}
              value={form.amount}
              onChange={(e) => set("amount", Number(e.target.value))}
            />
            <div className="spread">
              <span className="hint">{usd(1000)}</span>
              <span className="hint">{usd(50000)}</span>
            </div>

            <div className="field-row" style={{ marginTop: 8 }}>
              <Field label="Term (months)">
                <select
                  value={form.term_months}
                  onChange={(e) => set("term_months", e.target.value)}
                >
                  {["12", "24", "36", "48", "60"].map((t) => (
                    <option key={t} value={t}>
                      {t} months
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Purpose">
                <select
                  value={form.purpose}
                  onChange={(e) => set("purpose", e.target.value)}
                >
                  {PURPOSES.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <p className="hint" style={{ marginTop: 12 }}>
              Estimated rate {pct(OFFER_RATE_PCT)} APR (illustrative — your final
              rate is set at offer).
            </p>
          </>
        )}

        {/* ---- Step 4: Review ----------------------------------------- */}
        {step === 4 && (
          <>
            <StepHeader
              eyebrow="Step 4 of 5"
              title="Review your application"
              desc="Double check everything below before you submit."
            />
            <SummaryGroup title="Personal" onEdit={() => editStep(1)}>
              <SummaryRow label="Full name" value={form.name} />
              <SummaryRow label="Date of birth" value={form.dob} />
              <SummaryRow label="SSN" value={maskSsn(form.ssn)} />
              <SummaryRow label="Email" value={form.email} />
              <SummaryRow label="Phone" value={form.phone} />
              <SummaryRow label="Street address" value={form.street} />
              <SummaryRow label="City" value={form.city} />
              <SummaryRow label="State" value={form.state} />
              <SummaryRow label="ZIP code" value={form.zip_code} />
            </SummaryGroup>
            <SummaryGroup title="Employment & income" onEdit={() => editStep(2)}>
              <SummaryRow label="Employer" value={form.employer} />
              <SummaryRow label="Job title" value={form.job_title} />
              <SummaryRow
                label="Annual income"
                value={usd(form.annual_income)}
              />
              <SummaryRow
                label="Years at employer"
                value={form.employment_years}
              />
            </SummaryGroup>
            <SummaryGroup title="Loan details" onEdit={() => editStep(3)}>
              <SummaryRow label="Amount" value={usd(form.amount)} />
              <SummaryRow
                label="Term"
                value={`${form.term_months} months`}
              />
              <SummaryRow
                label="Purpose"
                value={
                  PURPOSES.find((p) => p.value === form.purpose)?.label ||
                  form.purpose
                }
              />
            </SummaryGroup>

            {apiError ? (
              <div className="alert alert-error">{apiError}</div>
            ) : null}

            <button
              className="btn-block"
              onClick={submitApplication}
              disabled={busy}
            >
              {busy ? "Submitting…" : "Submit application"}
            </button>
          </>
        )}

        {/* ---- Step 5: Decision & Offer ------------------------------- */}
        {step === 5 && (
          <>
            <StepHeader
              eyebrow="Step 5 of 5"
              title="Decision & offer"
              desc="Your identity is verified and your application is on its way to underwriting."
            />

            {!app ? (
              <div className="alert alert-warn">
                Submit your application in the previous step to continue.
              </div>
            ) : (
              <>
                <div className="alert alert-info">
                  Application <strong>#{String(app.app_id)}</strong> received.
                </div>

                {app.kyc ? (
                  <>
                    <h3 style={{ marginTop: 18 }}>Identity verification (KYC)</h3>
                    <div className="dl">
                      <KycRow
                        label="Name"
                        ok={app.kyc.name_verified}
                      />
                      <KycRow label="Date of birth" ok={app.kyc.dob_verified} />
                      <KycRow label="Address" ok={app.kyc.address_verified} />
                      <KycRow label="SSN" ok={app.kyc.ssn_verified} />
                    </div>
                  </>
                ) : null}

                <hr className="divider" />

                {!decision ? (
                  <button onClick={getDecision} disabled={busy}>
                    {busy ? "Evaluating…" : "Get decision"}
                  </button>
                ) : (
                  <>
                    <div className="spread">
                      <h3 style={{ margin: 0 }}>Underwriting decision</h3>
                      <StatusChip status={decision.decision} />
                    </div>
                    {typeof decision.score === "number" ? (
                      <p className="hint">Model score: {decision.score}</p>
                    ) : null}
                    {decision.adverse_action_reason ? (
                      <div className="alert alert-warn">
                        <strong>Adverse action reason:</strong>{" "}
                        {decision.adverse_action_reason}
                      </div>
                    ) : null}

                    {decisionApproved && !disclosure ? (
                      <button
                        style={{ marginTop: 16 }}
                        onClick={viewOffer}
                        disabled={busy}
                      >
                        {busy ? "Preparing offer…" : "View your offer"}
                      </button>
                    ) : null}
                  </>
                )}

                {disclosure ? (
                  <OfferPanel
                    disclosure={disclosure}
                    amount={form.amount}
                    termMonths={form.term_months}
                    showSchedule={showSchedule}
                    onToggleSchedule={() => setShowSchedule((v) => !v)}
                    onAccept={acceptOffer}
                    busy={busy}
                    acceptedLoanId={acceptedLoanId}
                  />
                ) : null}

                {apiError ? (
                  <div className="alert alert-error">{apiError}</div>
                ) : null}
              </>
            )}
          </>
        )}

        {/* ---- Wizard nav (steps 1-4) -------------------------------- */}
        {step < 4 && (
          <div className="btn-row between">
            <button
              className="btn-ghost"
              onClick={back}
              disabled={step === 1}
            >
              Back
            </button>
            {returningToReview ? (
              // Came here from the review: offer the one-click way home rather
              // than making the user press Next through the remaining steps.
              // Deliberately NOT "Save and return" -- there is no save boundary
              // here. Every field is a controlled input writing straight to
              // `form`, so an edit has already taken effect the moment it is
              // typed; this button only navigates (after re-validating).
              <button onClick={returnToReview}>Return to review</button>
            ) : (
              <button onClick={next}>Next</button>
            )}
          </div>
        )}
        {step === 4 && (
          <div className="btn-row">
            <button className="btn-ghost" onClick={back} disabled={busy}>
              Back
            </button>
          </div>
        )}
        </div>

        {showAside && (
          <aside className="apply-aside">
            <div className="card aside-card">
              <div className="card-title" style={{ marginBottom: 14 }}>
                Why this is safe
              </div>
              <ul className="aside-list">
                <li className="aside-item">
                  <span className="aside-icon">
                    <LockIcon />
                  </span>
                  <div>
                    <p className="aside-item-title">Encrypted end to end</p>
                    <p className="aside-item-desc">
                      Your personal information is protected in transit and
                      only used to process this application.
                    </p>
                  </div>
                </li>
                <li className="aside-item">
                  <span className="aside-icon">
                    <ShieldIcon />
                  </span>
                  <div>
                    <p className="aside-item-title">No obligation</p>
                    <p className="aside-item-desc">
                      Reviewing your decision and offer doesn&rsquo;t commit
                      you to anything — nothing is final until you accept.
                    </p>
                  </div>
                </li>
                <li className="aside-item">
                  <span className="aside-icon">
                    <ClockIcon />
                  </span>
                  <div>
                    <p className="aside-item-title">Takes about 3 minutes</p>
                    <p className="aside-item-desc">
                      Five short steps — identity, income, loan details,
                      review, and your decision.
                    </p>
                  </div>
                </li>
              </ul>
            </div>
          </aside>
        )}
      </div>
    </main>
  );
}

// ---- small presentational helpers ---------------------------------------

function StepHeader({
  eyebrow,
  title,
  desc,
  headingRef,
}: {
  eyebrow: string;
  title: string;
  desc: string;
  headingRef?: React.Ref<HTMLHeadingElement>;
}) {
  return (
    <div>
      <div className="step-eyebrow">{eyebrow}</div>
      {/* tabIndex={-1} makes the heading programmatically focusable without
          adding it to the tab order -- the standard pattern for announcing a
          view change to assistive tech. */}
      <h2 className="step-heading" ref={headingRef} tabIndex={-1}>{title}</h2>
      <p className="step-desc">{desc}</p>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-3.22 4.32M6.61 6.61C3.35 8.5 1 12 1 12s4 8 11 8a10.9 10.9 0 0 0 5.11-1.27" />
      <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" />
      <path d="M1 1l22 22" />
    </svg>
  );
}

function PinIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
      <circle cx="12" cy="10" r="3" />
    </svg>
  );
}

function LockIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 6v6l4 2" />
    </svg>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label>{label}</label>
      {children}
      {error ? <div className="field-error">{error}</div> : null}
    </div>
  );
}

function SummaryGroup({
  title,
  children,
  onEdit,
}: {
  title: string;
  children: React.ReactNode;
  onEdit?: () => void;
}) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        className="card-title"
        style={{
          marginBottom: 6,
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <span>{title}</span>
        {onEdit && (
          // Named for a screen reader too: four identical "Edit" buttons on one
          // screen are indistinguishable without the section name.
          <button
            type="button"
            className="btn-ghost"
            onClick={onEdit}
            aria-label={`Edit ${title}`}
            style={{ fontSize: "0.85rem", padding: "2px 10px" }}
          >
            Edit
          </button>
        )}
      </div>
      <div className="dl">{children}</div>
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="dl-row">
      <dt>{label}</dt>
      <dd>{value || "—"}</dd>
    </div>
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

function OfferPanel({
  disclosure,
  amount,
  termMonths,
  showSchedule,
  onToggleSchedule,
  onAccept,
  busy,
  acceptedLoanId,
}: {
  disclosure: Disclosure;
  amount: number;
  termMonths: string;
  showSchedule: boolean;
  onToggleSchedule: () => void;
  onAccept: () => void;
  busy: boolean;
  acceptedLoanId: string | number | null;
}) {
  const hasSchedule = !!disclosure.schedule && disclosure.schedule.length > 0;
  return (
    <div style={{ marginTop: 22 }}>
      <h3>Your offer</h3>
      <p className="hint" style={{ marginBottom: 12 }}>
        {usd(amount)} over {termMonths} months · monthly payment{" "}
        <strong>{usd(disclosure.monthly_payment)}</strong>
      </p>

      {/* Classic 4-box Federal Truth-in-Lending disclosure layout. */}
      <div className="tila">
        <div className="tila-title">Federal Truth-in-Lending Disclosure</div>
        <div className="tila-grid">
          <div className="tila-cell tila-cell-apr">
            <div className="tila-cell-label">Annual Percentage Rate</div>
            <div className="tila-cell-desc">
              The cost of your credit as a yearly rate.
            </div>
            <div className="tila-cell-value">{pct(disclosure.apr)}</div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Finance Charge</div>
            <div className="tila-cell-desc">
              The dollar amount the credit will cost you.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.finance_charge)}
            </div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Amount Financed</div>
            <div className="tila-cell-desc">
              The amount of credit provided to you.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.amount_financed)}
            </div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Total of Payments</div>
            <div className="tila-cell-desc">
              What you will have paid after all payments are made.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.total_of_payments)}
            </div>
          </div>
        </div>
      </div>

      {hasSchedule ? (
        <div style={{ marginTop: 16 }}>
          <button className="collapse-toggle" onClick={onToggleSchedule}>
            {showSchedule ? "Hide" : "Show"} payment schedule (
            {disclosure.schedule!.length})
          </button>
          {showSchedule ? (
            <div className="table-wrap table-scroll" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Due date</th>
                    <th className="num">Payment</th>
                    <th className="num">Principal</th>
                    <th className="num">Interest</th>
                    <th className="num">Balance</th>
                  </tr>
                </thead>
                <tbody>
                  {disclosure.schedule!.map((r) => (
                    <tr key={r.n}>
                      <td>{r.n}</td>
                      <td>{r.due_date}</td>
                      <td className="num">{usd(r.payment)}</td>
                      <td className="num">{usd(r.principal)}</td>
                      <td className="num">{usd(r.interest)}</td>
                      <td className="num">{usd(r.balance)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}

      {acceptedLoanId ? (
        <div className="alert alert-success">
          Offer accepted. Loan <strong>#{String(acceptedLoanId)}</strong>{" "}
          created.{" "}
          <Link href={`/servicing/${acceptedLoanId}`}>
            Go to your loan account →
          </Link>
        </div>
      ) : (
        <button style={{ marginTop: 16 }} onClick={onAccept} disabled={busy}>
          {busy ? "Accepting…" : "Accept offer"}
        </button>
      )}
    </div>
  );
}
