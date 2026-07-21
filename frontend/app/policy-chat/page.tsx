"use client";

import PolicyChat from "../../components/PolicyChat";
import RequireRole from "../../components/RequireRole";

export default function PolicyChatPage() {
  return (
    <RequireRole allow={["csr", "underwriter", "admin"]}>
      <main className="wrap wrap-narrow">
        <h1>Policy Chat</h1>
        <p className="sub" style={{ marginBottom: 20 }}>
          Ask the lending policy documents a question.
        </p>
        <PolicyChat />
      </main>
    </RequireRole>
  );
}
