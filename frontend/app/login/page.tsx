"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiPost, roleHome, setSession, type SessionUser } from "../../lib/api";

const SEEDED = [
  { creds: "csr / password", role: "Servicing rep" },
  { creds: "underwriter / password", role: "Underwriter" },
  { creds: "admin / password", role: "Admin" },
  { creds: "maria / password", role: "Borrower" },
];

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = (await apiPost("/auth/login", { username, password })) as {
        token: string;
        user: SessionUser;
      };
      if (!res?.token) {
        throw new Error("Login failed — no token returned.");
      }
      setSession(res.token, res.user);
      // Role-based landing only — every role can still reach any route by URL
      // (UI-only routing; the server-side controls that matter live at the
      // gateway and in servicing -- `docs/DEBT.md` D8 is closed).
      router.push(roleHome(res.user?.role));
    } catch (err) {
      const msg =
        err && typeof err === "object" && "detail" in err
          ? String((err as { detail: unknown }).detail)
          : err instanceof Error
            ? err.message
            : "Invalid username or password.";
      setError(msg || "Invalid username or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap wrap-narrow">
      <h1>Log in</h1>
      <p className="sub">Access the Meridian servicing and origination tools.</p>

      <div className="card">
        <form onSubmit={submit}>
          <label htmlFor="username">Username</label>
          <input
            id="username"
            value={username}
            autoComplete="username"
            onChange={(e) => setUsername(e.target.value)}
          />

          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
          />

          {error ? <div className="alert alert-error">{error}</div> : null}

          <button className="btn-block" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title" style={{ marginBottom: 10 }}>
          Demo credentials
        </div>
        <div className="dl">
          {SEEDED.map((s) => (
            <div className="dl-row" key={s.creds}>
              <dt>{s.role}</dt>
              <dd>
                <code>{s.creds}</code>
              </dd>
            </div>
          ))}
        </div>
      </div>
    </main>
  );
}
