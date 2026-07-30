"use client";

import { useState } from "react";
import { apiPost } from "../lib/api";

interface PolicyAnswer {
  answerable: boolean;
  answer: string;
  source_chunk_id: string | null;
  source_text: string | null;
}

interface Turn {
  question: string;
  answer: PolicyAnswer;
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

// "underwriting_guidelines.md#1.0" -> "Underwriting Guidelines". The raw
// chunk id is an internal retrieval-position pointer (file#paragraph.split),
// not something a reader needs -- show a human-readable document name in the
// UI, keep the raw id only as a hover tooltip for anyone who does need it.
function friendlySource(chunkId: string): string {
  const docId = chunkId.split("#")[0].replace(/\.md$/, "");
  return docId
    .split(/[_-]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function PolicyChat() {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;

    setBusy(true);
    setError(null);
    try {
      const res = (await apiPost("/assistant/policy-chat", {
        question: q,
      })) as PolicyAnswer;
      setTurns((prev) => [...prev, { question: q, answer: res }]);
      setQuestion("");
    } catch (err) {
      setError(errMsg(err, "Could not get an answer."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="card-title" style={{ marginBottom: 8 }}>
        Policy Chat
      </div>
      <p className="hint" style={{ marginTop: 0, marginBottom: 16 }}>
        Ask about lending <strong>policy</strong> — fees, underwriting rules,
        eligibility. Not for a specific application, borrower, or loan status
        (use the application&apos;s own AI Summary for that). Each question is
        answered on its own from the policy corpus — this isn&apos;t a
        conversation with memory of earlier turns.
      </p>

      {turns.length > 0 ? (
        <div style={{ marginBottom: 16 }}>
          {turns.map((turn, i) => (
            <div
              key={i}
              style={{
                padding: "14px 0",
                borderTop: i > 0 ? "1px solid var(--line)" : undefined,
              }}
            >
              <p style={{ margin: "0 0 8px", fontWeight: 600, overflowWrap: "break-word" }}>
                {turn.question}
              </p>
              {turn.answer.answerable ? (
                <>
                  <p style={{ margin: 0, overflowWrap: "break-word" }}>{turn.answer.answer}</p>
                  {turn.answer.source_text ? (
                    <blockquote
                      style={{
                        margin: "10px 0 0",
                        padding: "8px 12px",
                        borderLeft: "3px solid var(--line)",
                        overflowWrap: "break-word",
                      }}
                    >
                      <p
                        className="hint"
                        style={{ margin: 0, fontStyle: "italic", whiteSpace: "pre-wrap" }}
                      >
                        &ldquo;{turn.answer.source_text}&rdquo;
                      </p>
                      {turn.answer.source_chunk_id ? (
                        <p
                          className="hint"
                          style={{ margin: "6px 0 0" }}
                          title={turn.answer.source_chunk_id}
                        >
                          — {friendlySource(turn.answer.source_chunk_id)}
                        </p>
                      ) : null}
                    </blockquote>
                  ) : null}
                </>
              ) : (
                <p
                  className="muted"
                  style={{ margin: 0, fontStyle: "italic", overflowWrap: "break-word" }}
                >
                  {turn.answer.answer}
                </p>
              )}
            </div>
          ))}
        </div>
      ) : null}

      {error ? (
        <div className="alert alert-error" style={{ marginBottom: 12 }}>
          {error}
        </div>
      ) : null}

      <form onSubmit={ask} className="spread" style={{ gap: 8 }}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. what is the late fee amount? (policy questions only)"
          disabled={busy}
          style={{ flex: 1 }}
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
