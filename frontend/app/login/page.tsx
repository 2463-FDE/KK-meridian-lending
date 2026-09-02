"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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

  /** Is this form's own JavaScript live yet?
   *
   * THE DEFECT THIS EXISTS FOR, and it is a user-facing one rather than a test
   * artifact. The markup is server-rendered, so the Sign in button exists and is
   * clickable before the client bundle has attached `submit` to the form. A
   * click that lands in that window is not handled the way the page intends:
   * the sign-in can reach the gateway and come back 200 while the client never
   * records the session, and because `submit`'s catch never runs either, NO
   * error is displayed. The person is left looking at the login form, having
   * apparently done nothing, with a valid server-side session they cannot use.
   *
   * Measured on the same stack, 20 sign-ins clicked as early as the browser
   * allows: `next` 15.1.3 lost 0, `next` 15.5.25 lost 6. The window is not new
   * -- it widened, because the newer build takes longer to become interactive.
   * That is why this surfaced during the SEC-11 upgrade rather than being
   * caused by it.
   *
   * Disabling the control until mount closes the window at the only place that
   * can close it: a disabled button submits nothing, so there is no unhandled
   * click to lose. It also removes the need for any caller -- a person or a
   * browser test -- to guess when the page is ready, because "enabled" now
   * means "will actually be handled".
   */
  const [interactive, setInteractive] = useState(false);
  useEffect(() => {
    setInteractive(true);
  }, []);

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

          <button className="btn-block" type="submit" disabled={busy || !interactive}>
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
