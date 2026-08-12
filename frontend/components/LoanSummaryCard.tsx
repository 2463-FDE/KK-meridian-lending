"use client";

import { useState } from "react";
import { apiPost } from "../lib/api";

// Mirrors services/loan-assistant/app/schemas.py ExternalSignal. The citation
// string is composed server-side from what the provider returned, never by the
// model, so it is rendered as given rather than reassembled here from the
// parts -- reassembling would put a second author on a grounded figure.
interface ExternalSignal {
  source: string;
  series_id: string;
  label: string;
  value: number;
  unit: string;
  period: string;
  url: string;
  citation: string;
}

interface LoanSummary {
  applicant_name: string;
  loan_amount: number;
  term_months: number;
  purpose: string;
  summary: string;
  flags: string[];
  // Optional in the type because an older response, or a provider that is off
  // or unreachable, simply omits it -- the summary is still valid without it.
  external_signals?: ExternalSignal[];
}

// The coloured risk chip is gone with the field behind it.
//
// It rendered a model-generated low/medium/high/decline as a policy-looking
// badge, and no published rule maps an application to a tier -- so a staff member
// in manual review saw a red chip that carried the authority of a decision and
// the provenance of a sentence. What belongs in that position is the
// deterministic outcome and model score from decision-service, which the
// underwriting screen already shows.

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
            <span className="hint">AI summary — not a decision</span>
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
          {/* The grounded half of the summary. Kept visibly separate from the
              model's prose above: this figure was published by a named source
              and is checkable at the link, and an officer weighing the summary
              needs to be able to tell those two things apart. Absent entirely
              when the provider is off or unreachable -- an empty section would
              imply something was withheld. */}
          {summary.external_signals && summary.external_signals.length > 0 ? (
            <div style={{ marginTop: 14, paddingTop: 10, borderTop: "1px solid var(--border)" }}>
              <div className="hint" style={{ marginBottom: 6 }}>
                External context (not model-generated)
              </div>
              <ul style={{ margin: 0 }}>
                {summary.external_signals.map((signal) => (
                  <li key={`${signal.source}-${signal.series_id}-${signal.period}`}>
                    {signal.citation}{" "}
                    <a href={signal.url} target="_blank" rel="noreferrer noopener">
                      Verify at {signal.source}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
