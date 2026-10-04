import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import vercelConfig from "../../vercel.json";
import { getDashboard, getHealth, pageUrl, streamRun, uploadInvoice } from "../api";
import { AUTH_REQUIRED, apiBase, getToken, setToken } from "../apiBase";
import { TokenGate } from "../components/TokenGate";

let calls: { url: string; init?: RequestInit }[] = [];
let status = 200;
let esUrls: string[] = [];

beforeEach(() => {
  calls = []; status = 200; esUrls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ ok: true }), { status, headers: { "Content-Type": "application/json" } });
  }));
  vi.stubGlobal("EventSource", class {
    static CLOSED = 2; readyState = 0; onopen = null; onerror = null;
    constructor(url: string) { esUrls.push(url); }
    addEventListener() {} close() {}
  });
  sessionStorage.clear();
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); sessionStorage.clear(); });

describe("local development (nothing set)", () => {
  it("keeps relative URLs, no Authorization header, and the same fetch calls as before", async () => {
    expect(apiBase()).toBe("");
    await getHealth();
    expect(calls[0].url).toBe("/api/health");
    expect(calls[0].init).toBeUndefined();
    expect(pageUrl("abc", 1)).toBe("/api/runs/abc/pages/1");
    streamRun("abc", { onAudit() {}, onQueued() {}, onEnd() {}, onRejected() {}, onConnection() {} });
    expect(esUrls).toEqual(["/api/runs/abc/events"]);
  });
});

describe("deployed (VITE_API_BASE set)", () => {
  it("prefixes every kind of URL with the backend's address", async () => {
    vi.stubEnv("VITE_API_BASE", "https://invoice-agent-api.onrender.com/");
    await getDashboard();
    await uploadInvoice(new File(["%PDF-1.4"], "a.pdf"));
    expect(calls.map((c) => c.url)).toEqual(["https://invoice-agent-api.onrender.com/api/dashboard?recent=8&review=5",
                                             "https://invoice-agent-api.onrender.com/api/runs"]);
    expect(pageUrl("abc", 2)).toBe("https://invoice-agent-api.onrender.com/api/runs/abc/pages/2");
    streamRun("abc", { onAudit() {}, onQueued() {}, onEnd() {}, onRejected() {}, onConnection() {} });
    expect(esUrls).toEqual(["https://invoice-agent-api.onrender.com/api/runs/abc/events"]);
  });

  it("sends the tab's access token as a header, and in the query where headers are impossible", async () => {
    setToken("tok en");
    await getHealth();
    expect((calls[0].init!.headers as Record<string, string>).Authorization).toBe("Bearer tok en");
    await uploadInvoice(new File(["%PDF-1.4"], "a.pdf"));
    expect(calls[1].init!.method).toBe("POST");
    expect((calls[1].init!.headers as Record<string, string>).Authorization).toBe("Bearer tok en");
    expect(pageUrl("abc", 1)).toBe("/api/runs/abc/pages/1?access_token=tok%20en");
    streamRun("abc", { onAudit() {}, onQueued() {}, onEnd() {}, onRejected() {}, onConnection() {} });
    expect(esUrls[0]).toBe("/api/runs/abc/events?access_token=tok%20en");
  });
});

describe("the access-token screen", () => {
  it("appears only after a 401, stores the token for this tab and reloads", async () => {
    const reload = vi.fn();
    render(<TokenGate reload={reload} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    status = 401;
    await act(async () => { await getHealth().catch(() => {}); });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText("That token was not accepted.")).toBeNull();
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(getToken()).toBe("secret");
    expect(reload).toHaveBeenCalledOnce();
    expect(localStorage.getItem("invoice-agent-access-token")).toBeNull();          // sessionStorage only
  });
  it("says so when the stored token was refused", () => {
    setToken("old");
    render(<TokenGate reload={() => {}} />);
    act(() => { window.dispatchEvent(new Event(AUTH_REQUIRED)); });
    expect(screen.getByText("That token was not accepted.")).toBeInTheDocument();
  });
});

describe("vercel.json", () => {
  it("builds with Vite and sends every app route to index.html", () => {
    const cfg = vercelConfig;
    expect(cfg.buildCommand).toBe("npm run build");
    expect(cfg.outputDirectory).toBe("dist");
    // first: /api/gmail/* proxied to Render, so the Gmail OAuth cookie is first-party (DEPLOY_GMAIL_FIX.md); then the SPA fallback
    expect(cfg.rewrites).toEqual([{ source: "/api/gmail/:path*", destination: "https://invoice-agent-api.onrender.com/api/gmail/:path*" },
                                  { source: "/((?!assets/).*)", destination: "/index.html" }]);
  });
});
