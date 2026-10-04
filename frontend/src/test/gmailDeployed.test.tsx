import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { App } from "../App";
import { apiFetch, apiUrl } from "../apiBase";
import vercel from "../../vercel.json";
import statusGoogle from "./fixtures/gmail_status_google.json";

// Gmail OAuth on the deployed site (DEPLOY_GMAIL_FIX.md): the Gmail endpoints go same-origin through Vercel (so the HttpOnly binding
// cookie is first-party), every other call keeps VITE_API_BASE, and a failed return from Google always leaves a visible message.

let urls: string[] = [];
function mockApi() {
  urls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(url);
    const reply = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
    const path = url.replace(/^https:\/\/[^/]+/, "");
    if (path.startsWith("/api/gmail/status")) return reply(statusGoogle);
    if (path.startsWith("/api/health")) return reply({ status: "ok", mode: "live", model: "m", session_spent_usd: "0", session_ceiling_usd: "1.00",
                                                       run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520, max_files_per_upload: 20 });
    if (path.startsWith("/api/review-queue")) return reply({ items: [], open_count: 2 });
    if (path.startsWith("/api/runs")) return reply({ runs: [] });
    return reply({});
  }));
}
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); window.history.replaceState(null, "", "/"); });

describe("returning from Google", () => {
  it("a failed return keeps its message visible after the app re-renders (the upload screen is not remounted)", async () => {
    mockApi();
    window.history.replaceState(null, "", "/invoices?gmail=error&code=binding_mismatch");
    render(<App />);
    const text = /The sign-in came back without this browser's connection check/;
    expect(await screen.findByText(text)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("2 open")).toBeInTheDocument());     // the review count re-rendered the app
    await waitFor(() => expect(urls.some((u) => u.includes("/api/health"))).toBe(true));
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(window.location.search).toBe("");                                               // the address bar is still cleaned
  });
  it("every error code has its own sentence; an unknown one still shows a message", async () => {
    mockApi();
    window.history.replaceState(null, "", "/invoices?gmail=error&code=something_new");
    render(<App />);
    expect(await screen.findByText("The Gmail connection did not complete. Try again.")).toBeInTheDocument();
  });
  it("the binding message no longer sends a deployed user to localhost", async () => {
    mockApi();
    window.history.replaceState(null, "", "/invoices?gmail=error&code=binding_mismatch");
    render(<App />);
    const msg = await screen.findByText(/connection check/);
    expect(msg.textContent).not.toContain("localhost");
  });
});

describe("where the calls go when deployed", () => {
  it("Gmail endpoints are same-origin (relative); everything else, the event stream and page images use VITE_API_BASE", async () => {
    vi.stubEnv("VITE_API_BASE", "https://invoice-agent-api.onrender.com");
    mockApi();
    await apiFetch("/api/gmail/status");
    await apiFetch("/api/gmail/oauth/start", { method: "POST" });
    await apiFetch("/api/dashboard?recent=8&review=5");
    await apiFetch("/api/runs", { method: "POST" });
    expect(urls).toEqual(["/api/gmail/status", "/api/gmail/oauth/start", "https://invoice-agent-api.onrender.com/api/dashboard?recent=8&review=5",
                          "https://invoice-agent-api.onrender.com/api/runs"]);
    expect(apiUrl("/api/runs/abc/events")).toBe("https://invoice-agent-api.onrender.com/api/runs/abc/events");
    expect(apiUrl("/api/runs/abc/pages/1")).toBe("https://invoice-agent-api.onrender.com/api/runs/abc/pages/1");
  });
  it("local development is unchanged: everything relative", async () => {
    mockApi();
    await apiFetch("/api/gmail/status");
    await apiFetch("/api/dashboard");
    expect(urls).toEqual(["/api/gmail/status", "/api/dashboard"]);
  });
  it("vercel.json proxies ONLY /api/gmail/* to Render, before the SPA fallback", () => {
    const rewrites = vercel.rewrites as { source: string; destination: string }[];
    expect(rewrites[0]).toEqual({ source: "/api/gmail/:path*", destination: "https://invoice-agent-api.onrender.com/api/gmail/:path*" });
    const proxied = rewrites.filter((r) => r.destination.startsWith("http"));
    expect(proxied).toHaveLength(1);                                     // no SSE, upload or other API traffic through Vercel
    expect(rewrites[rewrites.length - 1].destination).toBe("/index.html");
  });
});
