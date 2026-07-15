"use client";

import PolicyChat from "../../components/PolicyChat";

export default function PolicyChatPage() {
  return (
    <main className="wrap wrap-narrow">
      <h1>Policy Chat</h1>
      <p className="sub" style={{ marginBottom: 20 }}>
        Ask the lending policy documents a question.
      </p>
      <PolicyChat />
    </main>
  );
}
