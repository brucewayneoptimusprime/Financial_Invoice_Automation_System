// Where the API is and how to reach it (deployment). Local development sets nothing: the base is "" (relative URLs through the
// Vite proxy) and there is no token, so every request is exactly what it was before deployment support existed.
//
//   VITE_API_BASE   build-time: the backend's URL on Render, e.g. https://invoice-agent-api.onrender.com (no trailing slash needed)
//   access token    asked for once per browser tab when the backend answers 401; kept in sessionStorage, never in the bundle
//
// Gmail endpoints are the exception: they always use RELATIVE URLs. Deployed, Vercel proxies /api/gmail/* to Render (vercel.json), so
// the OAuth binding cookie set by /api/gmail/oauth/start is first-party on the site's own domain and comes back with Google's
// redirect to /api/gmail/oauth/callback (DEPLOY_GMAIL_FIX.md). The event stream, uploads and every other call go to Render directly.

const TOKEN_KEY = "invoice-agent-access-token";
export const AUTH_REQUIRED = "invoice-agent:auth-required";

export function apiBase(): string {
  const raw = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
  return raw.trim().replace(/\/+$/, "");
}

const SAME_ORIGIN_PREFIX = "/api/gmail/";

function baseFor(path: string): string {
  return path.startsWith(SAME_ORIGIN_PREFIX) ? "" : apiBase();
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || null;
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the token lives only as long as this page */
  }
}

// For URLs the browser fetches by itself (EventSource, <img>): they cannot send headers, so the token rides in the query.
export function apiUrl(path: string, withToken = false): string {
  const url = apiBase() + path;
  const token = withToken ? getToken() : null;
  return token ? `${url}${url.includes("?") ? "&" : "?"}access_token=${encodeURIComponent(token)}` : url;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = getToken();
  const opts = token ? { ...(init ?? {}), headers: { ...((init?.headers as Record<string, string>) ?? {}), Authorization: `Bearer ${token}` } } : init;
  const url = baseFor(path) + path;
  const res = opts === undefined ? await fetch(url) : await fetch(url, opts);
  if (res.status === 401) window.dispatchEvent(new Event(AUTH_REQUIRED));
  return res;
}
