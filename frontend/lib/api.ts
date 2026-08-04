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
  constructor(status: number, detail: string, code?: string) {
    super(detail || `Request failed (${status})`);
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
    if (data && typeof data === "object" && "detail" in data) {
      const d = (data as { detail: unknown }).detail;
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
    throw new ApiError(res.status, detail, code);
  }
  return data;
}

export async function apiGet(path: string) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    cache: "no-store",
    headers: { ...authHeaders() },
  });
  return parse(res);
}

export async function apiPost(path: string, body?: unknown) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...authHeaders() },
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
