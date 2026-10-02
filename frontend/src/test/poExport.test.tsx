import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { POListScreen } from "../screens/POList";
import { PODetailScreen } from "../screens/PODetail";
import { filenameFrom } from "../download";
import { AUTH_REQUIRED } from "../apiBase";
import poList from "./fixtures/po_list.json";
import matched from "./fixtures/po_detail_matched.json";

// Export purchase orders (EXPORT_PLAN, stage E3): "Export" with a download icon, the per-PO menu (one component for the row and the
// PO page), the list's tick-boxes, the summary export, and the Blob download with the server's file name.

let calls: { url: string; init?: RequestInit }[] = [];
let exportStatus = 200;

function mockFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.includes("/export")) {
      if (exportStatus !== 200) {
        return new Response(JSON.stringify({ error: "unauthorized", message: "An access token is required." }),
                            { status: exportStatus, headers: { "Content-Type": "application/json" } });
      }
      return new Response("file-bytes", { status: 200, headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": "attachment; filename=\"PO-SS-001-full-20261003-1000.csv\"; filename*=UTF-8''PO-SS-001-full-20261003-1000.csv" } });
    }
    const body = url.startsWith("/api/pos/") ? matched : url.startsWith("/api/pos") ? (() => {
      const status = new URL(url, "http://x").searchParams.get("status");
      return { pos: status ? poList.pos.filter((p) => p.status === status) : poList.pos };
    })() : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

let saved: string[] = [];
beforeEach(() => {
  exportStatus = 200;
  saved = [];
  // jsdom has no object URLs: give the real URL class two mock functions for the test, removed again afterwards
  Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:x"), configurable: true, writable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true, writable: true });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { saved.push(this.download); });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (URL as unknown as Record<string, unknown>).createObjectURL;
  delete (URL as unknown as Record<string, unknown>).revokeObjectURL;
  window.history.replaceState(null, "", "/");
});

const exportCalls = () => calls.filter((c) => c.url.includes("/export")).map((c) => c.url);

async function list() {
  mockFetch();
  render(<POListScreen />);
  await screen.findByRole("link", { name: "PO-SS-001" });
}

async function choose(menuButton: HTMLElement, format: string, level?: string) {
  fireEvent.click(menuButton);
  if (level) fireEvent.click(screen.getByLabelText(level));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: format })); });
}

describe("naming", () => {
  it("says Export with a download icon, and nothing in the UI says Share", async () => {
    await list();
    const summary = screen.getByRole("button", { name: /Export all 6 shown/ });
    expect(summary.querySelector("svg.export-icon")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Export PO-SS-001" })).toHaveTextContent("Export");
    const sources = import.meta.glob("../**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const offenders = Object.entries(sources).filter(([path, text]) => !path.includes("/test/") && />[^<]*\bShare\b/.test(text)).map(([p]) => p);
    expect(offenders).toEqual([]);
  });
});

describe("one PO: the same component in the row and on the PO page", () => {
  it("the row's Export menu asks for level and format and saves the server's file name", async () => {
    await list();
    await choose(screen.getByRole("button", { name: "Export PO-SS-001" }), "Excel (.xlsx)", "Full (with metadata)");
    expect(exportCalls()).toEqual(["/api/pos/1/export?format=xlsx&level=full"]);
    expect(saved).toEqual(["PO-SS-001-full-20261003-1000.csv"]);
    expect(screen.queryByRole("group", { name: "Export PO-SS-001" })).toBeNull();          // the menu closes after the download
  });

  it("the PO page's Export button builds exactly the same request as the row's", async () => {
    await list();
    await choose(screen.getByRole("button", { name: "Export PO-SS-001" }), "PDF");
    const fromRow = exportCalls()[0];
    mockFetch();
    render(<PODetailScreen id={matched.po.id} />);
    const pageButton = await screen.findByRole("button", { name: `Export ${matched.po.po_number}` });
    await choose(pageButton, "PDF");
    expect(exportCalls()).toEqual([fromRow]);
    expect(fromRow).toBe(`/api/pos/${matched.po.id}/export?format=pdf&level=financial`);    // Financial is the default level
  });

  it("ticked rows never change a one-PO export", async () => {
    await list();
    fireEvent.click(screen.getByLabelText("Select PO-SS-002"));
    fireEvent.click(screen.getByLabelText("Select PO-SS-003"));
    await choose(screen.getByRole("button", { name: "Export PO-SS-001" }), "CSV");
    expect(exportCalls()).toEqual(["/api/pos/1/export?format=csv&level=financial"]);
  });
});

describe("clicks in a row never open the PO page", () => {
  it("clicking a row's Export button does not navigate", async () => {
    await list();
    const push = vi.spyOn(window.history, "pushState");
    fireEvent.click(screen.getByRole("button", { name: "Export PO-SS-003" }));
    expect(push).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
    expect(screen.getByRole("group", { name: "Export PO-SS-003" })).toBeInTheDocument();     // it opened its menu instead
  });

  it("clicking a row's tick-box does not navigate", async () => {
    await list();
    const push = vi.spyOn(window.history, "pushState");
    const box = screen.getByLabelText("Select PO-SS-003") as HTMLInputElement;
    fireEvent.click(box);
    expect(box.checked).toBe(true);
    expect(push).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });
});

describe("the summary export", () => {
  it("with nothing ticked it exports everything shown, with the active filter", async () => {
    await list();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "open" } });
    const button = await screen.findByRole("button", { name: /Export all 5 shown/ });
    await choose(button, "CSV");
    expect(exportCalls()).toEqual(["/api/pos/export?format=csv&status=open"]);
  });

  it("with rows ticked it exports only those (in the list's order) and says so on the button", async () => {
    await list();
    fireEvent.click(screen.getByLabelText("Select PO-SS-001"));
    fireEvent.click(screen.getByLabelText("Select PO-SS-004"));
    const button = screen.getByRole("button", { name: /Export 2 selected/ });
    await choose(button, "Word (.docx)");
    expect(exportCalls()).toEqual(["/api/pos/export?format=docx&ids=4%2C1"]);
  });

  it("select all shown ticks every visible row, and a partial selection shows as mixed", async () => {
    await list();
    const all = screen.getByLabelText("Select all shown") as HTMLInputElement;
    fireEvent.click(screen.getByLabelText("Select PO-SS-002"));
    expect(all.indeterminate).toBe(true);
    fireEvent.click(all);
    expect(screen.getByRole("button", { name: /Export 6 selected/ })).toBeInTheDocument();
    fireEvent.click(all);
    expect(screen.getByRole("button", { name: /Export all 6 shown/ })).toBeInTheDocument();
  });

  it("a tick on a row the filter no longer shows is dropped", async () => {
    await list();
    fireEvent.click(screen.getByLabelText("Select PO-SS-005"));                             // partially billed
    fireEvent.click(screen.getByLabelText("Select PO-SS-001"));                             // open
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "open" } });
    const button = await screen.findByRole("button", { name: /Export 1 selected/ });
    await choose(button, "PDF");
    expect(exportCalls()).toEqual(["/api/pos/export?format=pdf&status=open&ids=1"]);
  });
});

describe("the menu and the download", () => {
  it("opens and closes with aria-expanded, closes on Escape and returns focus", async () => {
    await list();
    const button = screen.getByRole("button", { name: "Export PO-SS-002" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("group", { name: "Format" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(button);
  });

  it("a refused export (401) shows the message, asks for the token and saves nothing", async () => {
    exportStatus = 401;
    await list();
    const asked = vi.fn();
    window.addEventListener(AUTH_REQUIRED, asked);
    await choose(screen.getByRole("button", { name: "Export PO-SS-001" }), "CSV");
    window.removeEventListener(AUTH_REQUIRED, asked);
    expect(await screen.findByRole("alert")).toHaveTextContent("An access token is required.");
    expect(asked).toHaveBeenCalled();
    expect(saved).toEqual([]);
  });

  it("the file name comes from Content-Disposition and is reduced to a plain name", () => {
    expect(filenameFrom("attachment; filename=\"a.csv\"; filename*=UTF-8''b%20c.pdf", "x")).toBe("b-c.pdf");
    expect(filenameFrom('attachment; filename="../../evil.exe"', "x")).toBe("evil.exe");
    expect(filenameFrom(null, "fallback.csv")).toBe("fallback.csv");
    expect(filenameFrom('attachment; filename="..."', "fallback.csv")).toBe("fallback.csv");
  });
});

describe("waitFor sanity", () => {
  it("the list renders the export column for every row", async () => {
    await list();
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Export PO-/ })).toHaveLength(6));
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("checkbox")).toHaveLength(7);                          // 6 rows + select all
  });
});
