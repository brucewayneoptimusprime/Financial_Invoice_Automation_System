import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { ERPSyncScreen } from "../screens/ERPSync";
import { POListScreen } from "../screens/POList";
import { PODetailScreen } from "../screens/PODetail";
import { parse } from "../router";
import { AUTH_REQUIRED } from "../apiBase";
import previewFx from "./fixtures/erp_preview.json";
import importFx from "./fixtures/erp_import.json";
import afterFx from "./fixtures/erp_preview_after.json";
import detailFx from "./fixtures/erp_po_detail.json";
import listFx from "./fixtures/erp_po_list.json";

// Simulated ERP feed (ERP_PLAN, stage R3): the button, the preview groups, ticking and confirming, the result, and the
// "Simulated ERP" labels in the list and on the PO page. Fixtures come from the real endpoints (tests/erp/frontend_fixtures.py).

let posts: { url: string; body: Record<string, unknown> }[] = [];
let previewReply: { status: number; body: unknown } = { status: 200, body: previewFx };
let importReply: { status: number; body: unknown } = { status: 200, body: importFx };
let previews = 0;

function mockFetch() {
  posts = [];
  previews = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
    if (url.startsWith("/api/erp/preview")) {
      previews += 1;
      return previews > 1 && previewReply.status === 200 ? reply(200, afterFx) : reply(previewReply.status, previewReply.body);
    }
    if (url.startsWith("/api/erp/import")) return reply(importReply.status, importReply.body);
    if (url.startsWith("/api/settings/pos/")) return reply(404, {});
    if (url.startsWith("/api/pos/")) return reply(200, detailFx);
    if (url.startsWith("/api/pos")) return reply(200, listFx);
    return reply(200, {});
  }));
}
afterEach(() => {
  vi.unstubAllGlobals();
  previewReply = { status: 200, body: previewFx };
  importReply = { status: 200, body: importFx };
  window.history.replaceState(null, "", "/");
});

const group = (title: RegExp) => screen.getByRole("heading", { name: title }).closest("section")!;

async function open() {
  mockFetch();
  render(<ERPSyncScreen />);
  await screen.findByRole("heading", { name: /New \(5\)/ });
}

describe("the route and the button", () => {
  it("parses /pos/erp-sync before /pos/:id", () => {
    expect(parse("/pos/erp-sync")).toEqual({ name: "erpSync" });
    expect(parse("/pos/12")).toEqual({ name: "po", id: 12 });
  });
  it("the PO list has a Sync from ERP button labelled as simulated", async () => {
    mockFetch();
    render(<POListScreen />);
    const link = await screen.findByRole("link", { name: "Sync from ERP (simulated)" });
    expect(link).toHaveAttribute("href", "/pos/erp-sync");
    expect(link).toHaveAttribute("title", expect.stringContaining("Simulated ERP (demo)"));
  });
});

describe("the preview", () => {
  it("is labelled Simulated ERP (demo) and names the feed, the format and the adapter", async () => {
    await open();
    expect(screen.getAllByText("Simulated ERP (demo)").length).toBeGreaterThan(0);
    expect(screen.getByTestId("erp-feed")).toHaveTextContent("erp_feed_sample.json");
    expect(screen.getByTestId("erp-feed")).toHaveTextContent("simerp.po-feed/v1 (adapter simerp-v1)");
    expect(screen.getByTestId("erp-feed")).toHaveTextContent("13 purchase orders");
    expect(posts).toEqual([]);                                        // the preview saves nothing
  });
  it("New: every new PO with its vendor, none ticked, warnings shown", async () => {
    await open();
    const g = group(/New \(5\)/);
    const boxes = within(g).getAllByRole("checkbox");
    expect(boxes).toHaveLength(5);
    boxes.forEach((b) => expect(b).not.toBeChecked());
    expect(within(screen.getByTestId("erp-new-4500012001")).getByText(/existing: SuperStore/)).toBeInTheDocument();
    expect(screen.getByTestId("erp-new-4500012003")).toHaveTextContent("(by tax ID)");
    const nw = screen.getByTestId("erp-new-4500012004");
    expect(nw).toHaveTextContent("new vendor: Northwind Office Supplies Ltd");
    expect(within(nw).getByText("status new")).toBeInTheDocument();
    expect(nw).toHaveTextContent(/will be created with status new/);
    expect(screen.getByTestId("erp-new-4500012005")).toHaveTextContent("EUR");
    expect(within(g).getByRole("button", { name: "Import 0 purchase orders" })).toBeDisabled();
  });
  it("Already exists: skipped, never changed, with a link to the stored PO", async () => {
    await open();
    const row = screen.getByTestId("erp-exists-PO-SS-005");
    expect(row).toHaveTextContent("is already stored and is skipped; it is never changed.");
    expect(within(row).getByRole("link", { name: "Open PO-SS-005" })).toHaveAttribute("href", expect.stringMatching(/^\/pos\/\d+$/));
    expect(within(group(/Already exists \(1\)/)).queryByRole("checkbox")).toBeNull();
  });
  it("Has problems: each reason, no tick-box, and the look-alike links to the existing PO", async () => {
    await open();
    const g = group(/Has problems \(7\)/);
    expect(within(g).queryByRole("checkbox")).toBeNull();
    const text = g.textContent ?? "";
    for (const reason of ["looks like existing PO-SS-002", "Currency is required", "5 x 19.99 = 99.95, not 109.95",
                          "quantity cannot be negative (-3)", "appears more than once in the feed", "CANCELLED in the ERP"]) {
      expect(text).toContain(reason);
    }
    expect(within(g).getByRole("link", { name: "Open existing PO-SS-002" })).toHaveAttribute("href", expect.stringMatching(/^\/pos\/\d+$/));
    expect(within(g).getAllByText("4500012009")).toHaveLength(2);
  });
});

describe("ticking, confirming and the result", () => {
  it("imports exactly the ticked POs after a confirm step, then shows the outcomes and a fresh preview", async () => {
    await open();
    fireEvent.click(screen.getByRole("checkbox", { name: "Import 4500012001" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Import 4500012004" }));
    fireEvent.click(screen.getByRole("button", { name: "Import 2 purchase orders" }));
    expect(posts).toEqual([]);                                        // nothing before the confirmation
    const confirm = screen.getByRole("group", { name: "Confirm import" });
    expect(confirm).toHaveTextContent("Save 2 purchase orders from the Simulated ERP (demo)?");
    await act(async () => { fireEvent.click(within(confirm).getByRole("button", { name: "Confirm import" })); });
    expect(posts).toEqual([{ url: "/api/erp/import",
                             body: { feed_sha256: previewFx.feed.sha256, po_numbers: ["4500012001", "4500012004"], confirm: true } }]);
    const result = await screen.findByRole("heading", { name: "Imported" });
    const section = result.closest("section")!;
    expect(within(section).getByRole("status")).toHaveTextContent("2 imported, 1 skipped (already exist).");
    expect(within(section).getByRole("link", { name: "4500012001" })).toHaveAttribute("href", expect.stringMatching(/^\/pos\/\d+$/));
    expect(section).toHaveTextContent("new vendor created (status new)");
    expect(section).toHaveTextContent("PO-SS-005 already exists");
    expect(await screen.findByRole("heading", { name: /New \(3\)/ })).toBeInTheDocument();     // the fresh preview after the import
    expect(screen.getByRole("heading", { name: /Already exists \(3\)/ })).toBeInTheDocument();
  });
  it("Cancel leaves the confirm step without importing", async () => {
    await open();
    fireEvent.click(screen.getByRole("checkbox", { name: "Import 4500012002" }));
    fireEvent.click(screen.getByRole("button", { name: "Import 1 purchase order" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Import 1 purchase order" })).toBeInTheDocument();
    expect(posts).toEqual([]);
  });
  it("a changed feed shows the server's message and the fresh preview, with nothing ticked", async () => {
    importReply = { status: 409, body: { error: "feed_changed", message: "The ERP feed changed since the preview; check the new preview and pick again.",
                                         preview: afterFx } };
    await open();
    fireEvent.click(screen.getByRole("checkbox", { name: "Import 4500012002" }));
    fireEvent.click(screen.getByRole("button", { name: "Import 1 purchase order" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm import" })); });
    expect(screen.getByRole("alert")).toHaveTextContent("The ERP feed changed since the preview");
    expect(screen.getByRole("heading", { name: /New \(3\)/ })).toBeInTheDocument();
    screen.getAllByRole("checkbox").forEach((b) => expect(b).not.toBeChecked());
  });
  it("a feed the server cannot read is a message, not a crash", async () => {
    previewReply = { status: 422, body: { error: "unknown_format", message: "The feed format 'x' has no adapter (known: simerp.po-feed/v1)." } };
    mockFetch();
    render(<ERPSyncScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent("has no adapter");
    expect(screen.getByText("Simulated ERP (demo)")).toBeInTheDocument();
  });
  it("a missing access token asks for it", async () => {
    previewReply = { status: 401, body: { error: "unauthorized", message: "An access token is required." } };
    mockFetch();
    const seen: string[] = [];
    const listener = () => seen.push("auth");
    window.addEventListener(AUTH_REQUIRED, listener);
    render(<ERPSyncScreen />);
    await screen.findByRole("alert");
    window.removeEventListener(AUTH_REQUIRED, listener);
    expect(seen.length).toBeGreaterThan(0);
  });
});

describe("provenance", () => {
  it("the PO list's Entered column says Simulated ERP feed", async () => {
    mockFetch();
    render(<POListScreen />);
    const row = (await screen.findByRole("link", { name: "4500012001" })).closest("tr")!;
    expect(row).toHaveTextContent("Simulated ERP feed");
    const seeded = screen.getByRole("link", { name: "PO-SS-001" }).closest("tr")!;
    expect(seeded).toHaveTextContent("Seed");
  });
  it("the PO page's Where this PO came from shows the ERP feed, the sync time and the ERP facts", async () => {
    mockFetch();
    render(<PODetailScreen id={detailFx.po.id} />);
    const section = (await screen.findByRole("heading", { name: "Where this PO came from" })).closest("section")!;
    expect(section).toHaveTextContent("Entered bySimulated ERP feed");
    expect(within(section).getByText("Simulated ERP (demo)")).toBeInTheDocument();
    expect(section).toHaveTextContent("Feederp_feed_sample.json");
    expect(section).toHaveTextContent("ERP statusRELEASED");
    expect(section).toHaveTextContent("Buyer referenceREQ-7781 / J. Rao");
    expect(section).toHaveTextContent("Units of measureline 1: EA");
    expect(within(section).getByText("Synced")).toBeInTheDocument();
  });
});
