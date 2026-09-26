import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { DashboardScreen } from "../screens/Dashboard";
import { PODetailScreen } from "../screens/PODetail";
import { App } from "../App";
import { parse } from "../router";
import busy from "./fixtures/dashboard_busy.json";
import empty from "./fixtures/dashboard_empty.json";
import matched from "./fixtures/po_detail_matched.json";

function mockFetch(dashboard: unknown, other: Record<string, unknown> = {}) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const reply = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/api/dashboard")) return reply(dashboard);
    for (const [prefix, body] of Object.entries(other)) if (url.startsWith(prefix)) return reply(body);
    if (url.startsWith("/api/health")) return reply({ status: "ok", mode: "replay", model: "claude-sonnet-5", session_spent_usd: "0",
                                                       session_ceiling_usd: "5.00", run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520 });
    if (url.startsWith("/api/review-queue")) return reply({ items: [], open_count: 1 });
    if (url.startsWith("/api/runs")) return reply({ runs: [] });
    return reply({});
  }));
  // jsdom has no EventSource; the run page opens one. A silent stand-in is enough for navigation tests.
  vi.stubGlobal("EventSource", class { onopen = null; onerror = null; readyState = 0; static CLOSED = 2; addEventListener() {} close() {} });
}
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("routes and navigation", () => {
  it("puts the dashboard at / and the upload screen at /invoices", () => {
    expect(parse("/")).toEqual({ name: "dashboard" });
    expect(parse("/invoices")).toEqual({ name: "upload" });
  });
  it("highlights Dashboard on / and Invoices on /invoices and on a run page", async () => {
    mockFetch(busy);
    const { unmount } = render(<App />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveClass("active");
    expect(within(nav).getByRole("link", { name: "Invoices" })).not.toHaveClass("active");
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    unmount();
    for (const path of ["/invoices", "/runs/abc123"]) {
      window.history.replaceState(null, "", path);
      const r = render(<App />);
      const n = screen.getByRole("navigation", { name: "Main" });
      expect(within(n).getByRole("link", { name: "Invoices" })).toHaveClass("active");
      expect(within(n).getByRole("link", { name: "Dashboard" })).not.toHaveClass("active");
      r.unmount();
    }
  });
});

describe("the dashboard", () => {
  it("shows the counts, the system decisions and the outcome after review", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    const processed = (await screen.findByText("Invoices processed")).parentElement!;
    expect(processed).toHaveTextContent("4");
    const decisions = screen.getByText("Decisions (by the system, at run time)").parentElement!;
    expect(within(decisions).getByText("Approve").closest("li")).toHaveTextContent(/^Approve\s*1$/);
    expect(within(decisions).getByText("Review").closest("li")).toHaveTextContent(/^Review\s*2$/);
    expect(decisions).toHaveTextContent("Now, after review: 2 approved, 0 rejected, 1 still in review, 1 awaiting information.");
    const bar = decisions.querySelector(".decision-bar")!;
    expect(bar.getAttribute("aria-hidden")).toBe("true");
    expect(bar.querySelectorAll(".seg")).toHaveLength(3);                     // approve, review, request_info (reject is 0)
    expect(screen.getByText("Review queue").closest("a")).toHaveAttribute("href", "/review");
  });

  it("shows the PO totals once per currency", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    const usd = (await screen.findByText(/^USD · 5 POs$/)).parentElement!;
    const inr = screen.getByText(/^INR · 1 PO$/).parentElement!;
    expect(usd).toHaveTextContent("39,500.00 USD");
    expect(inr).toHaveTextContent("5,000.00 INR");
    expect(usd).not.toHaveTextContent("INR");
  });

  it("lists recent runs and open review items with links", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    const runs = (await screen.findByRole("heading", { name: "Recent runs" })).closest("section")!;
    const links = within(runs).getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("/runs/"));
    expect(links).toHaveLength(4);
    expect(within(runs).getByText("now Approved")).toBeInTheDocument();       // 10963: review at run time, approved by a reviewer
    const review = screen.getByRole("heading", { name: "Waiting for review" }).closest("section")!;
    expect(within(review).getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("/review/"))).toHaveLength(1);
  });

  it("has an empty state that points to the upload screen", async () => {
    mockFetch(empty);
    render(<DashboardScreen />);
    expect(await screen.findByText(/No invoices yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "upload one" })).toHaveAttribute("href", "/invoices");
    expect(screen.queryByText(/Now, after review/)).toBeNull();
  });
});

describe("moved links", () => {
  it("the PO page's upload link goes to /invoices with the PO", async () => {
    mockFetch(busy, { "/api/pos/": matched });
    render(<PODetailScreen id={1} />);
    expect(await screen.findByRole("link", { name: "Upload invoices" })).toHaveAttribute("href", "/invoices?po=1");
  });
});

describe("clickable summaries", () => {
  it("decision chips and bar segments open the filtered Invoices list; Review opens the review queue", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    const decisions = (await screen.findByText("Decisions (by the system, at run time)")).parentElement!;
    const chipLinks = within(decisions).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(chipLinks).toEqual(["/invoices?decision=approve", "/review", "/invoices?decision=request_info", "/invoices?decision=reject"]);
    const segs = [...decisions.querySelectorAll(".decision-bar a.seg")].map((a) => a.getAttribute("href"));
    expect(segs).toEqual(["/invoices?decision=approve", "/review", "/invoices?decision=request_info"]);
    expect(decisions.querySelector(".decision-bar a.seg")!.getAttribute("tabindex")).toBe("-1");   // the chips are the keyboard links
  });

  it("each waiting item opens its own review screen", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    const review = (await screen.findByRole("heading", { name: "Waiting for review" })).closest("section")!;
    const itemLinks = within(review).getAllByRole("link").filter((a) => a.classList.contains("run-link"));
    expect(itemLinks.map((a) => a.getAttribute("href"))).toEqual(busy.review.oldest_open.map((i) => `/review/${i.id}`));
  });

  it("each currency card opens the PO list filtered to that currency", async () => {
    mockFetch(busy);
    render(<DashboardScreen />);
    expect((await screen.findByText(/^USD · 5 POs$/)).closest("a")).toHaveAttribute("href", "/pos?currency=USD");
    expect(screen.getByText(/^INR · 1 PO$/).closest("a")).toHaveAttribute("href", "/pos?currency=INR");
  });
});
