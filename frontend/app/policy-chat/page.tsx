"use client";

import PolicyChat from "../../components/PolicyChat";
import RequireRole from "../../components/RequireRole";

export default function PolicyChatPage() {
  // Staff-gated here, and the gateway agrees. This gate came first and stood
  // alone for a while: `assistant_policy_chat` resolved a session and proxied
  // regardless, so a borrower this screen refused was answered by the route.
  // Neither half was a bug on its own -- the gateway's own comment reasoned
  // that policy Q&A carries no per-applicant data -- so the disagreement was
  // recorded as `docs/DEBT.md` RF-28 rather than settled by whoever edited a
  // file last.
  //
  // The client has answered: Policy Chat is an INTERNAL tool for lending,
  // compliance and underwriting staff. The gateway now refuses an anonymous
  // caller with 401 and a non-staff session with 403, so this list and that
  // route are two halves of one decision. Widening either one alone would
  // recreate exactly the split RF-28 recorded.
  //
  // A borrower-facing policy assistant, if it is ever wanted, is a separate
  // surface with its own corpus and its own route -- not this page with the
  // gate removed.
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
