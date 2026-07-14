"use client";

import { useState } from "react";
import { apiPost } from "../lib/api";

interface LoanSummary {
  applicant_name: string;
  loan_amount: number;
  term_months: number;
  purpose: string;
  risk_tier: "low" | "medium" | "high" | "decline";
  summary: string;
  flags: string[];
}

const RISK_TONE: Record<LoanSummary["risk_tier"], string> = {
  low: "chip-green",
  medium: "chip-amber",
  high: "chip-red",
  decline: "chip-red-muted",
};

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function LoanSummaryCard({
  appId,
}: {
  appId: string | number;
}) {
  const [summary, setSummary] = useState<LoanSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      const res = (await apiPost(
        `/assistant/applications/${appId}/summary`
      )) as LoanSummary;
      setSummary(res);
    } catch (err) {
      setError(errMsg(err, "Could not generate an AI summary."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: summary ? 16 : 0 }}>
        <div>
          <div className="card-title" style={{ marginBottom: 8 }}>
            Application Summary
          </div>
          <p className="hint" style={{ margin: 0 }}>
            Generate a risk-tiered, plain-English summary of this application.
          </p>
        </div>
        <button onClick={generate} disabled={busy}>
          {busy ? "Generating…" : summary ? "Regenerate summary" : "Generate AI Summary"}
        </button>
      </div>

      {error ? (
        <div className="alert alert-error" style={{ marginTop: 12 }}>
          {error}
        </div>
      ) : null}

      {summary ? (
        <div>
          <div className="spread" style={{ marginBottom: 10 }}>
            <span className={`chip ${RISK_TONE[summary.risk_tier]}`}>
              {summary.risk_tier.toUpperCase()}
            </span>
          </div>
          <p style={{ marginTop: 0 }}>{summary.summary}</p>
          {summary.flags.length > 0 ? (
            <ul style={{ marginTop: 10 }}>
              {summary.flags.map((flag, i) => (
                <li key={i}>{flag}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
