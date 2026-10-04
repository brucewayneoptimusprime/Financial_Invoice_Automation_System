# Gmail sign-in on the deployed site: what was wrong and how to switch the fix on

Branch `deploy`. The fix is commit `0c9e1e1`; this guide is in the commit after it. Nothing else changed on the deployment.

- **Frontend (Vercel):** https://financial-invoice-automation-system-mauve.vercel.app
- **Backend (Render):** https://invoice-agent-api.onrender.com

## (a) What was wrong, and what changed

**Cause 1: the connection-check cookie was never stored** (your hypothesis, in substance).

- `POST /api/gmail/oauth/start` sets the HttpOnly cookie `gmail_oauth_binding`. The callback compares it with what the server remembered when the sign-in started. Without it, the callback refuses with `binding_mismatch` and never asks Google for a token. That part works as designed and stays.
- On the deployed site the frontend called the backend on **another site** (`vercel.app` to `onrender.com`). A browser stores a cookie from a cross-site `fetch` only if the request uses `credentials: "include"` and the server allows credentials in CORS. Neither was true. Browsers also increasingly block such third-party cookies outright, whatever their `SameSite` value.
- So the cookie never existed, and every return from Google ended in `binding_mismatch`. `SameSite=Lax` alone would not have stopped it: Lax cookies are sent on Google's top-level redirect. The cookie was rejected earlier, when it was set.

**Cause 2: the error message vanished at once.**

- The Gmail panel read `?gmail=error&code=…`, removed it from the address bar, and showed the sentence.
- But the upload screen is keyed on the address-bar query. When the app re-rendered a moment later (its first background fetch), the key had changed, so the screen was rebuilt and the message was lost. That is the "no visible error". A test reproduced it before the fix.

**What changed:**

| Where | Change |
|---|---|
| `frontend/vercel.json` | A rewrite **before** the SPA fallback: `/api/gmail/:path*` → `https://invoice-agent-api.onrender.com/api/gmail/:path*`. Only the Gmail endpoints go through Vercel. The live event stream, uploads, page images and every other API call still go straight to Render. |
| `frontend/src/apiBase.ts` | Gmail endpoints use relative URLs, so they are same-origin on the Vercel domain. Everything else keeps `VITE_API_BASE`. Local development is unchanged: everything was already relative there. |
| Backend `routes_gmail.py` | The cookie is now also `Secure` when `GMAIL_REDIRECT_URI` is https. HttpOnly, SameSite=Lax, its path and the 10-minute lifetime are unchanged. Local `http://localhost` behaves exactly as before. |
| Backend `callback.py` | The server's start-up check no longer warns about an https callback, but still checks the path. |
| `frontend/src/App.tsx`, `GmailImport.tsx` | The upload screen's key ignores `?gmail=` / `?code=`, so the result sentence stays visible. The `binding_mismatch` sentence no longer tells a deployed user to open localhost. |

**Unchanged:** single-use state, PKCE (S256), the binding check (constant-time, required), the read-only `gmail.readonly` scope (anything else is revoked and refused), the `ACCESS_TOKEN` gate on every Gmail route except the callback, and the tests that pin them. Tests now also cover:
- the deployed flow on the Vercel origin;
- a missing cookie giving `binding_mismatch` with no token request;
- the local cookie not being Secure;
- which calls go where;
- the visible message.

**Changed existing assertions:**
1. `frontend/src/test/deploy.test.tsx` "vercel.json" and `backend/tests/test_deploy_files.py::test_vercel_json`: the rewrites list now starts with the Gmail rule (the SPA rule is unchanged).
2. `backend/tests/gmail/test_stage1_schema_crypto.py::test_callback_problems`: an `https://…/api/gmail/oauth/callback` URI is no longer reported as "local callback only". Two cases were added: a wrong path on https, and plain http on a non-localhost host, both still warned.
3. `backend/tests/test_deploy_files.py::test_the_approved_settings`: `plan: free` → `plan: starter`. My previous commit `41e2203` changed render.yaml without running the suite, so this test was failing on `deploy` until now.

**Tests on `0c9e1e1`:** backend 2599 passed, 0 failed (4 live tests deselected); vitest 179 passed; tsc clean; build OK.

## (b) Google Cloud: the redirect URI to add

Google Cloud Console → **APIs & Services → Credentials** → your OAuth 2.0 Client ID (type *Web application*) → **Authorized redirect URIs** → **Add URI**:

```
https://financial-invoice-automation-system-mauve.vercel.app/api/gmail/oauth/callback
```

Keep `http://localhost:8000/api/gmail/oauth/callback` as well, so local development keeps working. No "Authorized JavaScript origin" is needed. **Save.** Google can take a few minutes to apply it.

While the consent screen is in **Testing**, the Google account you will connect must be listed under **OAuth consent screen → Test users**.

## (c) Render: environment variables

Service `invoice-agent-api` → **Environment**. Set the non-secret values exactly:

| Name | Value |
|---|---|
| `GMAIL_REDIRECT_URI` | `https://financial-invoice-automation-system-mauve.vercel.app/api/gmail/oauth/callback` |
| `GMAIL_UI_RETURN_URL` | `https://financial-invoice-automation-system-mauve.vercel.app/invoices` |
| `API_CORS_ORIGINS` | `https://financial-invoice-automation-system-mauve.vercel.app` (if it isn't already; the non-Gmail calls still go cross-site) |

The secrets must also be set. You enter their values yourself; never paste them anywhere else:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `OAUTH_ENCRYPTION_KEY`

Leave `GMAIL_BACKEND` **unset**. Gmail import is then real exactly when all three secrets are present. Keep `OAUTH_ENCRYPTION_KEY` the same from now on: a different key makes a stored connection unreadable, and the panel then asks you to connect again.

Saving these variables makes Render **redeploy**, which resets the database (see (f)).

## (d) Vercel: deploy the `deploy` branch and make it production

1. **Settings → Environment Variables:** `VITE_API_BASE` stays `https://invoice-agent-api.onrender.com`, unchanged.
2. **Deployments:** find the newest deployment of branch **`deploy`**, at commit `0c9e1e1` or later. It is built automatically on push.
   - If it isn't there, open the latest `deploy` deployment → **⋯ → Redeploy**, without the build cache.
3. Make it production:
   - If your production branch is `deploy` (Settings → Git → Production Branch), it already is.
   - Otherwise open that deployment → **⋯ → Promote to Production**.
4. Check that the production domain, `financial-invoice-automation-system-mauve.vercel.app`, now serves this deployment.
   - The rewrite and the redirect URI only line up on **this** domain.
   - Preview URLs (`…-git-deploy-….vercel.app`) will not complete the Gmail sign-in, because Google sends you back to the production domain.

## (e) How to test

1. **The proxy works.** Open `https://financial-invoice-automation-system-mauve.vercel.app/invoices` and enter the access token when asked. In the browser's developer tools (Network tab):
   - the Gmail panel's call is to `…vercel.app/api/gmail/status`, with no `onrender.com`, and answers 200;
   - other calls, such as `/api/runs`, still go to `invoice-agent-api.onrender.com`.
2. **Connect.** Click **Connect Gmail (read-only)**. Before you approve at Google, check Application → Cookies → `https://financial-invoice-automation-system-mauve.vercel.app`: a `gmail_oauth_binding` cookie with Path `/api/gmail/oauth`, HttpOnly ✓, Secure ✓, SameSite Lax.
3. **Approve at Google** with the test-user account. You come back to `/invoices`, the address bar is clean, and the panel says **"Gmail is connected (read-only)."** with your address and "read-only" in its header.
4. **Search and import** one attachment. It appears under "This upload" and runs like an upload.
5. **If it fails,** the panel now shows a sentence instead of nothing:
   - **"The sign-in came back without this browser's connection check…":** the cookie still did not arrive. Check (d)4: are you on the production domain? Is the newest `deploy` build live? Are cookies allowed?
   - **"Google did not complete the connection":** usually a redirect-URI mismatch. Compare (b) and `GMAIL_REDIRECT_URI` character for character.
   - **"Gmail import is not set up on the server":** one of the three secrets is missing on Render.
   - **Google shows "redirect_uri_mismatch" itself:** the URI in (b) is not saved yet, or Google hasn't applied it yet.

## (f) Warning: every Render redeploy wipes the Gmail connection

The Render service has **no disk**. The database is rebuilt from the demo data on **every** redeploy and restart, and the stored Gmail connection goes with it, along with every run, review, setting and imported PO. Saving an environment variable also redeploys.

So, in this order:
1. Google Cloud (b).
2. All Render variables (c), and wait until that deploy is live.
3. Vercel (d).
4. **Connect Gmail last** (e).

After any later Render change, connect Gmail again. The Starter plan no longer spins down when idle, so the connection survives quiet periods, but not a redeploy or restart.
