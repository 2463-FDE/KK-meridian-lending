"use client";

/**
 * Manual DTI evidence, on the application a reviewer is actually looking at.
 *
 * RF-25. The client authorised staff to apply DTI manually on a REFERRED
 * application, as an underwriter or admin, from approved SYNTHETIC source
 * documents -- recording income, obligations, the documents, the calculation,
 * who assessed it, their role, when, and why. A bare percentage is explicitly
 * insufficient, which is why this form cannot submit one.
 *
 * WHAT THIS PANEL IS CAREFUL NOT TO BE
 *
 * It is not a decision control. Recording evidence approves nothing, denies
 * nothing and changes no decision -- the API writes to `manual_dti_*` and to
 * nothing else -- so the copy says that in the panel rather than leaving a
 * reviewer to infer it from a form that sits directly above the approve/deny
 * control. A screen that let someone believe they had decided something would
 * be worse than no screen.
 *
 * NO RATIO IS COMPUTED IN THIS BROWSER. There is deliberately no live preview
 * of the DTI as the two figures are typed. The server does not compute the
 * ratio either -- Postgres evaluates the same expression the CHECK constraint
 * verifies the stored row against -- and a preview here would be a THIRD
 * definition of the calculation, in the place least able to stay in step with
 * the other two. The figure appears once, after it is recorded, read back from
 * the response. Rendering stored basis points as a percentage is presentation;
 * deriving them from two inputs would not be.
 *
 * ROLE. Rendered only for underwriter and admin. A CSR is staff elsewhere on
 * this page and is not authorised here, and offering a form whose every
 * submission would be refused is worse than not offering one. The gateway and
 * the database enforce this regardless of what this component renders.
 */
import { useCallback, useEffect, useState } from "react";

import { apiGet, apiPost, getUser } from "../../../lib/api";

/** Roles the client authorised for manual DTI. Not the page's staff set. */
const AUTHORISED_ROLES = ["underwriter", "admin"];

interface SourceDocument {
  doc_ref: string;
  kind: string;
  label: string;
}

interface Assessment {
  id: number;
  app_id: number;
  assessed_by: number;
  assessed_role: string;
  gross_monthly_income: string;
  monthly_debt_obligations: string;
  dti_bp: number;
  reason: string;
  assessed_at: string;
  documents: SourceDocument[];
}

function errMsg(err: unknown, fallback: string): string {
  const message = err instanceof Error ? err.message : "";
  return message || fallback;
}

/**
 * Basis points as a percentage, to two places.
 *
 * Presentation of a stored integer, not a calculation: 3360 is 33.60%. The
 * recorded figure stays exact in the database and this never rounds it into
 * something a reader could not reconcile with the two inputs shown beside it.
 */
function pct(basisPoints: number): string {
  return `${(basisPoints / 100).toFixed(2)}%`;
}

function money(value: string): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  return n.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  });
}

function shortDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export default function ManualDtiPanel({
  appId,
  isReferred,
}: {
  appId: string;
  /** Whether the application is currently in the referred state. */
  isReferred: boolean;
}) {
  const role = getUser()?.role ?? null;
  const authorised = role !== null && AUTHORISED_ROLES.includes(role);

  const [documents, setDocuments] = useState<SourceDocument[]>([]);
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [income, setIncome] = useState("");
  const [obligations, setObligations] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [recorded, setRecorded] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!authorised || !appId) return;
    setLoadError(null);
    try {
      // Two requests, one failure surface: the register is the part a reviewer
      // must be able to read, so a registry that will not load must not blank
      // the evidence already recorded.
      const rows = (await apiGet(
        `/los/applications/${appId}/manual-dti`
      )) as Assessment[];
      setAssessments(rows);
    } catch (err) {
      setAssessments([]);
      setLoadError(errMsg(err, "The manual DTI register could not be read."));
    }
    try {
      const docs = (await apiGet(
        "/los/manual-dti/source-documents"
      )) as SourceDocument[];
      setDocuments(docs);
    } catch {
      setDocuments([]);
    }
  }, [appId, authorised]);

  useEffect(() => {
    void load();
  }, [load]);

  function toggle(ref: string) {
    setSelected((prev) =>
      prev.includes(ref) ? prev.filter((r) => r !== ref) : [...prev, ref]
    );
  }

  async function submit() {
    setBusy(true);
    setFormError(null);
    setRecorded(null);
    try {
      const created = (await apiPost(`/los/applications/${appId}/manual-dti`, {
        // Sent as typed, as strings. Parsing to a JavaScript number here would
        // put a binary float between the reviewer's figures and the NUMERIC
        // columns they are stored in (D12), for no benefit -- the server
        // validates them and the database is what enforces the shape.
        gross_monthly_income: income.trim(),
        monthly_debt_obligations: obligations.trim(),
        document_refs: selected,
        reason: reason.trim(),
      })) as Assessment;
      // The RECORDED ratio, read back rather than computed here.
      setRecorded(
        `Recorded: ${pct(created.dti_bp)} from ${money(
          created.gross_monthly_income
        )} income and ${money(created.monthly_debt_obligations)} obligations.`
      );
      setIncome("");
      setObligations("");
      setSelected([]);
      setReason("");
      await load();
    } catch (err) {
      setFormError(errMsg(err, "The assessment could not be recorded."));
    } finally {
      setBusy(false);
    }
  }

  if (!authorised) return null;

  const canSubmit =
    !busy &&
    income.trim() !== "" &&
    obligations.trim() !== "" &&
    selected.length > 0 &&
    reason.trim() !== "";

  return (
    <div className="card" style={{ marginTop: 16 }} data-testid="manual-dti">
      <div className="card-title" style={{ marginBottom: 8 }}>
        Manual DTI evidence
      </div>
      <p className="hint" style={{ marginBottom: 16 }}>
        A recorded assessment is <strong>evidence for a human reviewer</strong>.
        It does not approve, deny or change this application&rsquo;s decision,
        and the underwriting model never sees it. Use the decision control above
        to resolve the referral.
      </p>

      {loadError ? <div className="alert alert-error">{loadError}</div> : null}

      {assessments.length > 0 ? (
        <div style={{ marginBottom: 20 }} data-testid="manual-dti-register">
          {assessments.map((a) => (
            <div
              key={a.id}
              className="card"
              style={{ marginBottom: 12 }}
              data-testid="manual-dti-assessment"
            >
              <div className="spread">
                <strong data-testid="manual-dti-ratio">{pct(a.dti_bp)}</strong>
                <span className="hint">
                  {a.assessed_role} · {shortDateTime(a.assessed_at)}
                </span>
              </div>
              <p className="hint" style={{ marginTop: 6 }}>
                {money(a.gross_monthly_income)} gross monthly income against{" "}
                {money(a.monthly_debt_obligations)} of monthly obligations.
              </p>
              <p className="hint">
                Documents:{" "}
                {a.documents.map((d) => d.doc_ref).join(", ") || "—"}
              </p>
              <p className="hint">{a.reason}</p>
            </div>
          ))}
        </div>
      ) : (
        <p className="hint" style={{ marginBottom: 20 }}>
          No manual DTI has been recorded for this application.
        </p>
      )}

      {isReferred ? (
        <>
          <label htmlFor="dti-income">Gross monthly income</label>
          <input
            id="dti-income"
            inputMode="decimal"
            value={income}
            onChange={(e) => setIncome(e.target.value)}
            placeholder="6250.00"
          />
          <label htmlFor="dti-obligations">Monthly debt obligations</label>
          <input
            id="dti-obligations"
            inputMode="decimal"
            value={obligations}
            onChange={(e) => setObligations(e.target.value)}
            placeholder="2100.00"
          />

          <label>Source documents</label>
          <p className="hint" style={{ marginBottom: 8 }}>
            Approved synthetic documents only. At least one is required — a
            ratio with no evidence behind it is not evidence.
          </p>
          <div style={{ marginBottom: 12 }}>
            {documents.length === 0 ? (
              <p className="hint">
                The approved document registry could not be read, so nothing can
                be cited right now.
              </p>
            ) : (
              documents.map((d) => (
                <label
                  key={d.doc_ref}
                  style={{ display: "block", fontWeight: 400 }}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(d.doc_ref)}
                    onChange={() => toggle(d.doc_ref)}
                    style={{ width: "auto", marginRight: 8 }}
                  />
                  {d.doc_ref} — {d.label}
                </label>
              ))
            )}
          </div>

          <label htmlFor="dti-reason">Reason</label>
          <textarea
            id="dti-reason"
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Income from the synthetic paystub; obligations from the synthetic schedule"
          />

          {formError ? (
            <div className="alert alert-error" data-testid="manual-dti-error">
              {formError}
            </div>
          ) : null}
          {recorded ? (
            <div
              className="alert alert-success"
              role="status"
              data-testid="manual-dti-recorded"
            >
              {recorded}
            </div>
          ) : null}

          <button
            style={{ marginTop: 14 }}
            onClick={submit}
            disabled={!canSubmit}
            data-testid="manual-dti-submit"
          >
            {busy ? "Recording…" : "Record assessment"}
          </button>
        </>
      ) : (
        // The register stays readable after the referral is resolved: an
        // assessment is a permanent audit fact, and hiding it once a decision
        // lands would remove the evidence somebody may have relied on.
        <p className="hint" data-testid="manual-dti-closed">
          This application is not referred, so no new assessment can be
          recorded. Anything already recorded stays on the record above.
        </p>
      )}
    </div>
  );
}
