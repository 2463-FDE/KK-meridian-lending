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
// UI, keep the raw id available for anyone who does need it.
function friendlySource(chunkId: string): string {
  const docId = chunkId.split("#")[0].replace(/\.md$/, "");
  return docId
    .split(/[_-]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

//: Questions worth asking first, and each one is answerable from the corpus.
//
// Chips that produce a refusal would teach the opposite lesson on the first
// click -- that the assistant does not know things -- when the point of the
// refusal path is that it declines what it cannot GROUND. There is a test that
// these are answerable, so a chip cannot quietly rot into a demo of the
// failure case.
const EXAMPLES = [
  "What is the late fee?",
  "What score requires manual review?",
  "What loan terms are available?",
];

export default function PolicyChat() {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which turns have their evidence open. Held by index because a turn has no
  // id -- the list only ever grows or is cleared wholesale.
  const [openEvidence, setOpenEvidence] = useState<Record<number, boolean>>({});

  async function submit(q: string) {
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

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    await submit(question.trim());
  }

  // Clears what this browser is DISPLAYING and nothing else. There is no
  // server-side conversation to end: each question is answered on its own from
  // the corpus, and the service keeps no history between turns. This button is
  // deliberately not a "new conversation" control, because that would imply the
  // previous turns were context the model had.
  function clearSession() {
    setTurns([]);
    setOpenEvidence({});
    setError(null);
  }

  return (
    <div className="card">
      <div className="card-title" style={{ marginBottom: 8 }}>
        Policy Chat
      </div>
      <p className="hint" style={{ marginTop: 0, marginBottom: 16 }}>
        Ask questions about Meridian lending <strong>policy</strong> — fees,
        underwriting rules, eligibility. Not for a specific application,
        borrower, or loan status (use the application&apos;s own AI Summary for
        that). Each question is answered on its own from the policy corpus —
        this isn&apos;t a conversation with memory of earlier turns.
      </p>

      {turns.length === 0 ? (
        <div style={{ marginBottom: 16 }} data-testid="policy-chat-examples">
          <p className="hint" style={{ margin: "0 0 8px" }}>
            Try one of these:
          </p>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="btn-ghost btn-sm"
                disabled={busy}
                onClick={() => submit(example)}
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {turns.length > 0 ? (
        <div style={{ marginBottom: 16 }} data-testid="policy-chat-turns">
          {turns.map((turn, i) => (
            <div
              key={i}
              data-testid={`policy-turn-${i}`}
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
                  {/* The grounded badge is rendered ONLY when the server said
                      the answer is answerable AND sent the excerpt it came
                      from. Showing it on the strength of `answerable` alone
                      would let a claim of grounding appear with nothing behind
                      it, which is the one thing this panel must not do. */}
                  {turn.answer.source_text ? (
                    <p
                      className="muted"
                      data-testid={`policy-grounded-${i}`}
                      style={{ margin: "0 0 6px", fontSize: 12 }}
                    >
                      ✓ Grounded in policy
                    </p>
                  ) : null}
                  <p style={{ margin: 0, overflowWrap: "break-word" }}>{turn.answer.answer}</p>

                  {turn.answer.source_chunk_id ? (
                    <p className="hint" style={{ margin: "8px 0 0" }}>
                      Source:{" "}
                      <strong data-testid={`policy-source-${i}`}>
                        {friendlySource(turn.answer.source_chunk_id)}
                      </strong>
                    </p>
                  ) : null}

                  {turn.answer.source_text ? (
                    <div style={{ marginTop: 6 }}>
                      {/* Collapsed by default: the answer is the point and the
                          excerpt is the proof behind it. Available in one
                          click, so "show me where that came from" never needs
                          a second screen. */}
                      <button
                        type="button"
                        className="btn-link"
                        aria-expanded={Boolean(openEvidence[i])}
                        data-testid={`policy-evidence-toggle-${i}`}
                        onClick={() =>
                          setOpenEvidence((prev) => ({ ...prev, [i]: !prev[i] }))
                        }
                      >
                        {openEvidence[i] ? "Hide evidence" : "Show evidence"}
                      </button>
                      {openEvidence[i] ? (
                        <blockquote
                          data-testid={`policy-evidence-${i}`}
                          style={{
                            margin: "8px 0 0",
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
                            /* The raw retrieval pointer, secondary on purpose:
                               `file#paragraph.split` is an internal position,
                               useful when checking the corpus and noise to
                               everyone else. */
                            <p
                              className="hint"
                              data-testid={`policy-chunk-${i}`}
                              style={{ margin: "6px 0 0", fontSize: 11, opacity: 0.8 }}
                            >
                              {turn.answer.source_chunk_id}
                            </p>
                          ) : null}
                        </blockquote>
                      ) : null}
                    </div>
                  ) : null}
                </>
              ) : (
                /* A refusal. No grounded badge, no evidence block, and the
                   server's own sentence rather than a paraphrase -- the whole
                   contract is that it declines what it cannot ground, and a
                   panel that dressed a refusal up would break it. */
                <p
                  className="muted"
                  data-testid={`policy-refusal-${i}`}
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

      {turns.length > 0 ? (
        <p className="hint" style={{ margin: "10px 0 0" }}>
          <button
            type="button"
            className="btn-link"
            data-testid="policy-clear-session"
            onClick={clearSession}
            disabled={busy}
          >
            Clear this session
          </button>{" "}
          — removes these turns from this browser. Nothing is stored on the
          server to clear: each question is answered on its own.
        </p>
      ) : null}
    </div>
  );
}
