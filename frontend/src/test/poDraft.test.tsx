import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { POForm } from "../components/POForm";
import { draftToForm, PONewScreen } from "../screens/PONew";
import type { Health, PODraftView } from "../types";
import textDraft from "./fixtures/po_draft_text.json";
import newVendorDraft from "./fixtures/po_draft_new_vendor.json";
import failedDraft from "./fixtures/po_draft_failed.json";
import vendors from "./fixtures/vendors.json";

const TEXT = textDraft as unknown as PODraftView;
const NEW_VENDOR = newVendorDraft as unknown as PODraftView;
const FAILED = failedDraft as unknown as PODraftView;
const health = (mode: Health["mode"]) => ({ status: "ok", mode, model: "claude-sonnet-5", session_spent_usd: "0", session_ceiling_usd: "5.00",
                                             run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520 }) as Health;

let calls: { url: string; init?: RequestInit }[] = [];
function mockFetch(draft: PODraftView | null, saveBody: unknown = { po_id: 7, vendor_id: 1, warnings: [] }) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (url === "/api/vendors") return reply(200, vendors);
    if (url === "/api/pos/validate") return reply(200, { issues: [], can_save: true, lines_sum: null });
    if (url.startsWith("/api/pos/drafts/")) return reply(200, draft);
    if (url === "/api/pos") return reply(201, saveBody);
    return reply(404, {});
  }));
}
afterEach(() => vi.unstubAllGlobals());

describe("draftToForm", () => {
  it("fills only what the draft stated and keeps the suggested vendor", () => {
    expect(draftToForm(TEXT)).toEqual({ po_number: "PO-7788", vendor_id: 1, currency: "USD", total: "1250.00", issued_date: "2026-01-15",
                                        lines: [{ description: "Office chairs", quantity: "5", unit_price: "250.00", amount: "" }] });
    const nv = draftToForm(NEW_VENDOR);
    expect(nv.vendor_id).toBeNull();
    expect(nv.currency).toBe("");                                      // not stated in the source: left empty, never guessed
  });
});

describe("POForm with a draft", () => {
  it("marks model-filled fields and fields not in the source", async () => {
    mockFetch(null);
    render(<POForm initial={draftToForm(NEW_VENDOR)} initialNewVendor={NEW_VENDOR.new_vendor} draftId={NEW_VENDOR.draft_id}
                   marks={NEW_VENDOR.marks} vendorHint={NEW_VENDOR.vendor_hint} onSaved={() => {}} />);
    const po = screen.getByLabelText(/^PO number/).closest("label")!;
    expect(within(po).getByText(/from the model/)).toBeInTheDocument();
    const currency = screen.getByLabelText(/^Currency/).closest("label")!;
    expect(within(currency).getByText("not in the source")).toBeInTheDocument();
    expect((screen.getByLabelText("New vendor name") as HTMLInputElement).value).toBe("Globex Corporation");
    expect(screen.getByText(/NEW vendor \(status new\) is proposed/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been saved yet/)).toBeInTheDocument();
  });
});

describe("New PO screen", () => {
  it("drafts from text, shows that nothing is saved, and saves only on the button with the draft id", async () => {
    mockFetch(TEXT);
    const pushState = vi.spyOn(window.history, "pushState");
    render(<PONewScreen health={health("replay")} />);
    fireEvent.click(screen.getByRole("tab", { name: "Describe in text" }));
    fireEvent.change(screen.getByLabelText("Describe the purchase order"), { target: { value: "PO-7788 to SuperStore ..." } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Draft the purchase order" })); });
    expect(await screen.findByText(/Nothing has been saved\./)).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/pos")).toBe(false);          // the draft alone never saves
    expect((screen.getByLabelText(/^PO number/) as HTMLInputElement).value).toBe("PO-7788");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save purchase order" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save purchase order" })); });
    const save = calls.find((c) => c.url === "/api/pos")!;
    expect(JSON.parse(String(save.init!.body)).draft_id).toBe(TEXT.draft_id);
    expect(JSON.parse(String(save.init!.body)).po.vendor_id).toBe(1);
    await waitFor(() => expect(pushState).toHaveBeenCalledWith(null, "", "/pos/7"));
  });

  it("a failed draft says so and offers the empty form", async () => {
    mockFetch(FAILED);
    render(<PONewScreen health={health("replay")} />);
    fireEvent.click(screen.getByRole("tab", { name: "Describe in text" }));
    fireEvent.change(screen.getByLabelText("Describe the purchase order"), { target: { value: "x" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Draft the purchase order" })); });
    expect(await screen.findByRole("alert")).toHaveTextContent("No recorded response");
    fireEvent.click(screen.getByRole("button", { name: "Use the empty form" }));
    expect(screen.getByRole("tab", { name: "Form" })).toHaveAttribute("aria-selected", "true");
  });

  it("uploads one document for a draft", async () => {
    mockFetch({ ...TEXT, source: "document" });
    render(<PONewScreen health={health("live")} />);
    fireEvent.click(screen.getByRole("tab", { name: "Upload a document" }));
    const file = new File(["%PDF-1.4"], "po.pdf", { type: "application/pdf" });
    await act(async () => { fireEvent.change(screen.getByTestId("po-file"), { target: { files: [file] } }); });
    expect(await screen.findByText(/from the document/)).toBeInTheDocument();
    const call = calls.find((c) => c.url === "/api/pos/drafts/document")!;
    expect(call.init!.body).toBeInstanceOf(FormData);
  });

  it("offline mode points to the form and makes no call", () => {
    mockFetch(TEXT);
    render(<PONewScreen health={health("offline")} />);
    fireEvent.click(screen.getByRole("tab", { name: "Describe in text" }));
    expect(screen.getByText(/Offline mode: no model is available/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Draft the purchase order" })).toBeNull();
    expect(calls.some((c) => c.url.startsWith("/api/pos/drafts"))).toBe(false);
  });
});
