export const GATEWAY_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000";

// ---- auth/session helpers (browser-only localStorage) --------------------

export interface SessionUser {
  id: string | number;
  username: string;
  role: "borrower" | "csr" | "underwriter" | "admin" | string;
  name: string;
}

const TOKEN_KEY = "meridian.token";
const USER_KEY = "meridian.user";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getUser(): SessionUser | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as SessionUser;
  } catch {
    return null;
  }
}

export function setSession(token: string, user: SessionUser) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

/**
 * Role -> landing route after login. Used by login redirect + nav.
 * UI-only routing convenience. The gateway/API still accept ANY authenticated
 * caller — server-side authz is intentionally absent (debt D8, fixed in W6).
 * A role can still navigate anywhere by URL; this only sets the default landing.
 */
export function roleHome(role: string | null | undefined): string {
  switch (role) {
    case "csr":
      return "/servicing";
    case "underwriter":
      return "/underwriting";
    case "admin":
      return "/admin";
    case "borrower":
    default:
      return "/";
  }
}

// ---- fetch helpers -------------------------------------------------------

export class ApiError extends Error {
  status: number;
  detail: string;
  // Bug fix (borrower offer workflow): some endpoints now return a
  // structured {"code", "message"} detail instead of a plain string, so a
  // caller can switch on a stable machine-readable reason instead of
  // parsing human text (e.g. offers.py's APPLICATION_NOT_APPROVED /
  // APPLICATION_ALREADY_BOARDED). Optional and additive -- every endpoint
  // that still returns a plain string detail behaves exactly as before;
  // `code` is simply undefined for those.
  code?: string;
  /**
   * The raw `detail` when it is an object.
   *
   * `detail` above is flattened to the human message, which is right for
   * display and lossy for everything else -- the resumable-intake failure
   * carries `app_id`, `access_token` and `resume_token` in that object, and the
   * client needs the token to retry rather than start a second application.
   */
  data?: Record<string, unknown>;
  constructor(status: number, detail: string, code?: string, data?: Record<string, unknown>) {
    super(detail || `Request failed (${status})`);
    this.data = data;
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    let detail: string;
    let code: string | undefined;
    let structured: Record<string, unknown> | undefined;
    if (data && typeof data === "object" && "detail" in data) {
      const d = (data as { detail: unknown }).detail;
      if (d && typeof d === "object") structured = d as Record<string, unknown>;
      if (d && typeof d === "object" && "message" in d) {
        // Structured {"code", "message"} detail -- see ApiError.code above.
        detail = String((d as { message: unknown }).message);
        code = "code" in d ? String((d as { code: unknown }).code) : undefined;
      } else {
        detail = String(d);
      }
    } else if (typeof data === "string" && data) {
      detail = data;
    } else {
      detail = `Request failed (${res.status})`;
    }
    throw new ApiError(res.status, detail, code, structured);
  }
  return data;
}

export async function apiGet(path: string, extraHeaders?: Record<string, string>) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    cache: "no-store",
    headers: { ...authHeaders(), ...extraHeaders },
  });
  return parse(res);
}


// ---- intake retry credentials (browser-only sessionStorage) ---------------
//
// A borrower whose KYC call fails is told to retry. Without a stable key that
// retry creates a SECOND applicant and a SECOND application -- one person, two
// borrower records. The key says WHICH draft is being retried; the resume token,
// issued by the server, authorises recovering it. Both are needed
// (db/migrations/0036, 0037).
//
// sessionStorage, not localStorage and not a URL:
//   - a URL leaks into access logs, browser history and Referer, which is the
//     finding the accept-token audit already produced on this codebase;
//   - sessionStorage is scoped to the tab and cleared when it closes, which
//     matches the lifetime of a draft application.
//
// Neither value is PII and neither is ever logged.
const INTAKE_KEY = "meridian.intake.idempotency_key";
const INTAKE_RESUME = "meridian.intake.resume_token";

/**
 * A CSPRNG value from sessionStorage, minted on first use.
 *
 * Both intake credentials are generated HERE, in the browser, before anything
 * is sent. That ordering is the whole point and is worth stating plainly: a
 * credential the server mints and returns is one the client does not have if
 * the RESPONSE is lost. The applicant then retries, cannot prove the draft is
 * theirs, is refused, and starts over -- creating the duplicate the
 * idempotency key exists to prevent. Minting before the first request means
 * the browser still holds the credential when the network fails mid-flight.
 */
function mintedOnce(storageKey: string): string {
  if (typeof window === "undefined") return "";
  const existing = window.sessionStorage.getItem(storageKey);
  if (existing) return existing;
  if (typeof crypto === "undefined" || !crypto.randomUUID) {
    // Deliberately no Date.now()+Math.random() fallback. That is guessable,
    // and one of these two values authorises access to an application. A
    // browser without the Web Crypto API gets no retry credential rather than
    // a weak one -- it loses recovery, not confidentiality.
    return "";
  }
  const value = crypto.randomUUID() + crypto.randomUUID().replace(/-/g, "");
  window.sessionStorage.setItem(storageKey, value);
  return value;
}

/** The key for the CURRENT draft, minted once and reused for every retry. */
export function intakeIdempotencyKey(): string {
  return mintedOnce(INTAKE_KEY);
}

/**
 * The recovery secret proving this browser started the draft.
 *
 * Sent as `X-Resume-Token` on EVERY submission including the first, so the
 * server can store its hash at creation time and compare it on any retry.
 */
export function intakeResumeToken(): string {
  return mintedOnce(INTAKE_RESUME);
}

/**
 * Clear both after the application completes.
 *
 * Leaving them would make the NEXT application in the same tab resume the
 * previous one -- the borrower would fill in a new form and be handed back their
 * old application.
 */
export function clearIntakeRetryCredentials() {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(INTAKE_KEY);
  window.sessionStorage.removeItem(INTAKE_RESUME);
}

export async function apiPost(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...authHeaders(), ...extraHeaders },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse(res);
}

export async function apiDelete(path: string) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "DELETE",
    cache: "no-store",
    headers: { ...authHeaders() },
  });
  return parse(res);
}
