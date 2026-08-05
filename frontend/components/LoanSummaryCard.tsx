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

// Regeneration only ever calls this read-only summarizer endpoint -- it
// never touches /review or /decision, so it structurally cannot change the
// application's decision, reason, staff member, or timestamp. If the
// application's underlying data hasn't changed, the model may reasonably
// return the same (or a near-identical) summary -- that's expected, not a
// bug, since summarization is deterministic-ish over unchanged inputs.
const REGENERATE_HINT = "Regenerate Summary — this will not change the final decision.";

export default function LoanSummaryCard({
  appId,
}: {
  appId: string | number;
}) {
  const [summary, setSummary] = useState<LoanSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [generatedAt, setGeneratedAt] = useState<Date | null>(null);

  async function generate() {
    setBusy(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const res = (await apiPost(
        `/assistant/applications/${appId}/summary`
      )) as LoanSummary;
      setSummary(res);
      const now = new Date();
      setGeneratedAt(now);
      setSuccessMsg(
        summary
          ? `Summary regenerated at ${now.toLocaleTimeString()}. The final decision was not changed.`
          : `Summary generated at ${now.toLocaleTimeString()}.`
      );
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
            {summary
              ? REGENERATE_HINT
              : "Generate a risk-tiered, plain-English summary of this application."}
          </p>
        </div>
        <button onClick={generate} disabled={busy} title={summary ? REGENERATE_HINT : undefined}>
          {busy ? "Generating…" : summary ? "Regenerate summary" : "Generate AI Summary"}
        </button>
      </div>

      {successMsg ? (
        <div className="alert alert-success" style={{ marginTop: 12, marginBottom: summary ? 12 : 0 }}>
          {successMsg}
        </div>
      ) : null}
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
            {generatedAt ? (
              <span className="hint">Generated {generatedAt.toLocaleString()}</span>
            ) : null}
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
