import { useEffect, useState } from "react";
import { AUTH_REQUIRED, getToken, setToken } from "../apiBase";

// Shown only when the backend answers 401, i.e. only when a deployment has ACCESS_TOKEN set. Locally it never appears.
// The token is kept for this browser tab (sessionStorage) and never written into the build.
export function TokenGate({ reload = () => window.location.reload() }: { reload?: () => void }) {
  const [open, setOpen] = useState(false);
  const [rejected, setRejected] = useState(false);
  const [value, setValue] = useState("");

  useEffect(() => {
    const on = () => { setRejected(getToken() !== null); setOpen(true); };
    window.addEventListener(AUTH_REQUIRED, on);
    return () => window.removeEventListener(AUTH_REQUIRED, on);
  }, []);

  if (!open) return null;
  return (
    <div className="viewer-backdrop">
      <form className="token-gate" role="dialog" aria-modal="true" aria-labelledby="token-h"
            onSubmit={(e) => { e.preventDefault(); if (value.trim()) { setToken(value.trim()); reload(); } }}>
        <h2 id="token-h">Access token</h2>
        <p className="dim">This deployment is protected. Enter the access token you were given; it is kept only for this browser tab.</p>
        {rejected && <p className="error" role="alert">That token was not accepted.</p>}
        <input type="password" autoComplete="off" aria-label="Token" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        <button type="submit" className="btn" disabled={!value.trim()}>Continue</button>
      </form>
    </div>
  );
}
