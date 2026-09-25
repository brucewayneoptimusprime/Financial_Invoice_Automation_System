import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { POForm } from "../components/POForm";
import { POListScreen } from "../screens/POList";
import { PODetailScreen } from "../screens/PODetail";
import { parse } from "../router";
import poList from "./fixtures/po_list.json";
import matched from "./fixtures/po_detail_matched.json";
import historic from "./fixtures/po_detail_historic.json";
import considered from "./fixtures/po_detail_considered.json";
import vendors from "./fixtures/vendors.json";

type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown };
let calls: { url: string; init?: RequestInit }[] = [];

function mockFetch(handler: Handler) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const { status = 200, body } = handler(url, init);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }));
}

beforeEach(() => vi.useRealTimers());
afterEach(() => vi.unstubAllGlobals());

const ok = { issues: [], can_save: true, lines_sum: null };

describe("routes", () => {
  it("parses the PO routes", () => {
    expect(parse("/pos")).toEqual({ name: "pos" });
    expect(parse("/pos/new")).toEqual({ name: "poNew" });
    expect(parse("/pos/12")).toEqual({ name: "po", id: 12 });
    expect(parse("/pos/abc")).toEqual({ name: "missing" });
  });
});

describe("PO list", () => {
  it("lists POs with derived balances and links", async () => {
    mockFetch(() => ({ body: poList }));
    render(<POListScreen />);
    const link = await screen.findByRole("link", { name: "PO-SS-005" });
    const row = link.closest("tr")!;
    expect(within(row).getByText("9,000.00 USD")).toBeInTheDocument();
    expect(within(row).getByText("7,500.00 USD")).toBeInTheDocument();
    expect(link.getAttribute("href")).toMatch(/^\/pos\/\d+$/);
  });
  it("sends the search to the API", async () => {
    mockFetch(() => ({ body: poList }));
    render(<POListScreen />);
    await screen.findByRole("link", { name: "PO-SS-005" });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "IQ" } });
    await waitFor(() => expect(calls.some((c) => c.url.includes("q=IQ"))).toBe(true));
  });
});

describe("PO detail", () => {
  it("shows the balance, the matched run with its decision and a link to it", async () => {
    mockFetch(() => ({ body: matched }));
    render(<PODetailScreen id={1} />);
    expect(await screen.findByRole("heading", { name: "PO-SS-001" })).toBeInTheDocument();
    expect(screen.getByText("Awaiting review").nextSibling?.textContent).toBe("5,338.08 USD");
    const inv = screen.getByRole("link", { name: "10963" });
    expect(inv.getAttribute("href")).toBe(`/runs/${matched.invoices[0].run_id}`);
    expect(within(inv.closest("tr")!).getByText("Review")).toBeInTheDocument();
  });
  it("marks a historic invoice and the ledger", async () => {
    mockFetch(() => ({ body: historic }));
    render(<PODetailScreen id={5} />);
    expect(await screen.findByText("historic, no run")).toBeInTheDocument();
    expect(screen.getByText("Commit")).toBeInTheDocument();
  });
  it("lists near misses as not matched", async () => {
    mockFetch(() => ({ body: considered }));
    render(<PODetailScreen id={2} />);
    expect(await screen.findByText("Also considered in (not matched)")).toBeInTheDocument();
    expect(screen.getByText("matched PO-SS-001")).toBeInTheDocument();
  });
});

describe("POForm (manual path)", () => {
  function setup(validate: (body: any) => unknown = () => ok, save?: Handler) {
    mockFetch((url, init) => {
      if (url === "/api/vendors") return { body: vendors };
      if (url === "/api/pos/validate") return { body: validate(JSON.parse(String(init?.body))) };
      if (url === "/api/pos" && save) return save(url, init);
      return { status: 404, body: {} };
    });
    const onSaved = vi.fn();
    render(<POForm onSaved={onSaved} />);
    return onSaved;
  }

  it("keeps Save disabled while validation reports an error", async () => {
    setup(() => ({ issues: [{ field: "po_number", level: "error", code: "required", message: "PO number is required." }], can_save: false, lines_sum: null }));
    expect(await screen.findByText("PO number is required.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save purchase order" })).toBeDisabled();
  });

  it("offers the sum of the lines but only applies it when pressed", async () => {
    setup(() => ({ issues: [{ field: "total", level: "warning", code: "lines_sum", message: "The lines add up to 100.00, not the total 90.00." }],
                   can_save: true, lines_sum: "100.00" }));
    const btn = await screen.findByRole("button", { name: "Use the sum of the lines (100.00)" });
    const total = screen.getByLabelText(/^Total/) as HTMLInputElement;
    expect(total.value).toBe("");
    fireEvent.click(btn);
    expect(total.value).toBe("100.00");
  });

  it("saves exactly what is on the form, then reports the new PO", async () => {
    const onSaved = setup(() => ok, () => ({ status: 201, body: { po_id: 42, vendor_id: 1, warnings: [] } }));
    await screen.findByRole("option", { name: "SuperStore" });
    fireEvent.change(screen.getByLabelText(/^PO number/), { target: { value: "PO-9" } });
    fireEvent.change(screen.getByLabelText("Vendor"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText(/^Currency/), { target: { value: "usd" } });
    fireEvent.change(screen.getByLabelText(/^Total/), { target: { value: "1,000.00" } });
    fireEvent.click(screen.getByRole("button", { name: "Add line" }));
    fireEvent.change(screen.getByLabelText("Line 1 description"), { target: { value: "Chairs" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save purchase order" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save purchase order" })); });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(42));
    const saveCall = calls.find((c) => c.url === "/api/pos")!;
    expect(JSON.parse(String(saveCall.init!.body))).toEqual({
      po: { po_number: "PO-9", vendor_id: 1, currency: "USD", total: "1,000.00", issued_date: null,
            lines: [{ description: "Chairs", quantity: null, unit_price: null, amount: null }] },
      new_vendor: null, draft_id: null });
  });

  it("creates a new vendor inline and says it will have status new", async () => {
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "New vendor…" }));
    expect(screen.getByText(/Created with status/)).toHaveTextContent("Created with status new");
    fireEvent.change(screen.getByLabelText("New vendor name"), { target: { value: "Acme" } });
    await waitFor(() => {
      const body = JSON.parse(String(calls.filter((c) => c.url === "/api/pos/validate").at(-1)!.init!.body));
      expect(body.new_vendor).toEqual({ name: "Acme", tax_id: null, country: null });
      expect(body.po.vendor_id).toBeNull();
    });
  });

  it("shows a refused save's reason and issues", async () => {
    setup(() => ok, () => ({ status: 409, body: { error: "duplicate", message: "A purchase order with this number already exists.",
      issues: [{ field: "po_number", level: "error", code: "duplicate", message: "PO number PO-SS-001 already exists." }] } }));
    fireEvent.change(screen.getByLabelText(/^PO number/), { target: { value: "PO-SS-001" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save purchase order" })).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save purchase order" })); });
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(screen.getByText("PO number PO-SS-001 already exists.")).toBeInTheDocument();
  });
});
