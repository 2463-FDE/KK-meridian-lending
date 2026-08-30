"use client";

import PolicyChat from "../../components/PolicyChat";
import RequireRole from "../../components/RequireRole";

export default function PolicyChatPage() {
  // Staff-gated HERE while the gateway allows the same route ANONYMOUSLY
  // (`gateway/app/main.py::assistant_policy_chat`, which says so explicitly:
  // policy Q&A carries no per-applicant data, so a borrower may ask without an
  // account). The two are not in agreement about who this feature is for, and
  // that is a product question rather than a bug in either half:
  //
  //   * `docs/ROADMAP.md` describes loan-assistant as "Policy Q&A (anyone)";
  //   * the same file's demo walkthrough says to log in as csr/underwriter/
  //     admin and visit `/policy-chat`, which is what this gate enforces.
  //
  // Neither statement is false -- one describes the API, the other describes
  // the screen -- so nothing here resolves whether a BORROWER should be able to
  // use it in the browser. This gate is therefore left exactly as it is:
  // loosening it would widen access on a guess, and tightening the gateway
  // would remove access the gateway deliberately granted. Recorded as a
  // decision in `docs/DEBT.md` (RF-28) instead of settled by whoever edited
  // this file last.
  return (
    <RequireRole allow={["csr", "underwriter", "admin"]}>
      <main className="wrap wrap-narrow">
        <h1>Policy Chat</h1>
        <p className="sub" style={{ marginBottom: 20 }}>
          Ask questions about Meridian lending policy.
        </p>
        <PolicyChat />
      </main>
    </RequireRole>
  );
}
