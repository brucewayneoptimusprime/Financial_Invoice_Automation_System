import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { allowanceCents, checkChoices, initialChoices, toCents } from "../allocation";
import { ReviewListScreen } from "../screens/ReviewList";
import { ReviewItemScreen } from "../screens/ReviewItem";
import { PODetailScreen } from "../screens/PODetail";
import type { NeedsInput, ReviewDetail } from "../types";
import list from "./fixtures/review_list.json";
import auto from "./fixtures/review_item_auto.json";
import ambiguous from "./fixtures/review_item_ambiguous.json";
import bundled from "./fixtures/review_item_bundled.json";
import blocked from "./fixtures/review_item_blocked.json";
import approved from "./fixtures/review_approved.json";
import invalid from "./fixtures/review_422_invalid.json";
import poUnassigned from "./fixtures/po_detail_unassigned.json";

const AUTO = auto as unknown as ReviewDetail;
const AMB = ambiguous as unknown as ReviewDetail;
const BUNDLED = bundled as unknown as ReviewDetail;
let calls: { url: string; body?: any }[] = [];

function mockFetch(detail: unknown, action?: { status: number; body: unknown }) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/api/review-queue?")) return reply(200, list);
    if (/\/api\/review-queue\/\d+$/.test(url)) return reply(200, detail);
    if (url.endsWith("/approve") || url.endsWith("/reject")) return reply(action?.status ?? 200, action?.body ?? { status: "rejected" });
    if (url.startsWith("/api/pos/")) return reply(200, detail);
    return reply(404, {});
  }));
}
afterEach(() => vi.unstubAllGlobals());

describe("the client fit check is the server's", () => {
  it("floors the percentage part and takes the lesser of pct and abs", () => {
    const tol = { pct: 2, abs: "50.00", mode: "lesser_of" as const };
    expect(allowanceCents(40000, tol)).toBe(800);                   // 8.00 on 400.00 (the server's message in the 422 fixture)
    expect(allowanceCents(98039, tol)).toBe(1960);                  // 19.60 on 980.39
    expect(allowanceCents(0, tol)).toBe(0);
    expect(allowanceCents(40000, { ...tol, mode: "greater_of" })).toBe(5000);
    expect(toCents("1,000.00")).toBeNull();
    expect(toCents("1000.5")).toBe(100050);
  });
  it("lets lines that choose the same PO line use it up in order", () => {
    const n = (id: number, no: number, amount: string): NeedsInput => ({
      invoice_line_id: id, invoice_line_no: no, description: "x", quantity: null, unit_price: null, amount, status: "no_match", why: "",
      suggested: { target: "unassigned" }, candidates: [],
      other_lines: [{ po_line_id: 7, po_line_no: 1, description: "p", unit_price: null, remaining_amount: "600.00", fits: true, code: null, remaining: "600.00", allowance: "12.00" }] });
    const needs = [n(1, 1, "400.00"), n(2, 2, "400.00")];
    const res = checkChoices(needs, { 1: { invoice_line_id: 1, target: "po_line", po_line_id: 7 }, 2: { invoice_line_id: 2, target: "po_line", po_line_id: 7 } },
                             { pct: 2, abs: "50.00", mode: "lesser_of" });
    expect(res[1].ok).toBe(true);
    expect(res[2]).toEqual({ ok: false, message: "400.00 does not fit PO line 1: 200.00 remaining, allowance 4.00." });
  });
  it("pre-selects the server's best guess", () => {
    expect(initialChoices(AMB.approve.needs_input)[AMB.approve.needs_input[0].invoice_line_id].target).toBe("po_line");
    expect(initialChoices(BUNDLED.approve.needs_input)[BUNDLED.approve.needs_input[0].invoice_line_id].target).toBe("unassigned");
  });
});

describe("the review queue", () => {
  it("lists open items with their state and offers no bulk controls", async () => {
    mockFetch(null);
    render(<ReviewListScreen />);
    const rows = await screen.findAllByRole("link");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("ready")).toBeInTheDocument();
    expect(within(rows[1]).getByText("1 line choice")).toBeInTheDocument();
    expect(document.querySelector("input[type=checkbox]")).toBeNull();
    expect(screen.queryByRole("button", { name: /approve|reject/i })).toBeNull();
  });
});

describe("approving an item", () => {
  it("all lines confident: one confirmation, the balance and the automatic allocation, and nothing to choose", async () => {
    mockFetch(AUTO, { status: 200, body: approved });
    render(<ReviewItemScreen id={1} />);
    expect(await screen.findByText("Allocated automatically")).toBeInTheDocument();
    expect(screen.getByText(/6,000.00 USD/)).toHaveTextContent(/6,000.00 USD.*661.92 USD/);
    expect(screen.queryByRole("radiogroup")).toBeNull();
    const confirm = screen.getByRole("button", { name: "Confirm approval" });
    expect(confirm).toBeEnabled();
    await act(async () => { fireEvent.click(confirm); });
    const sent = calls.find((c) => c.url.endsWith("/approve"))!.body;
    expect(sent).toEqual({ confirm: true, state_token: AUTO.approve.state_token, allocations: [], note: null });
    expect(await screen.findByRole("heading", { name: "Approved" })).toBeInTheDocument();
  });

  it("an ambiguous line: ranked candidates, the best guess pre-selected, a line that cannot fit disabled with its numbers", async () => {
    mockFetch(AMB, { status: 200, body: approved });
    render(<ReviewItemScreen id={2} />);
    const group = await screen.findByRole("radiogroup");
    const radios = within(group).getAllByRole("radio");
    expect(radios[0]).toBeChecked();                                   // PO line 1, the best match
    expect(radios[2]).toBeDisabled();                                  // Widget B cannot take 600.00
    expect(within(group).getByText(/does not fit \(400.00 remaining \+ 8.00 allowance\)/)).toBeInTheDocument();
    fireEvent.click(within(group).getByRole("radio", { name: /No specific line/ }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm approval" })); });
    const sent = calls.find((c) => c.url.endsWith("/approve"))!.body;
    expect(sent.allocations).toEqual([{ invoice_line_id: AMB.approve.needs_input[0].invoice_line_id, target: "unassigned" }]);
  });

  it("shows the server's per-line problem on the line", async () => {
    mockFetch(BUNDLED, { status: 422, body: invalid });
    render(<ReviewItemScreen id={3} />);
    await screen.findByRole("radiogroup");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm approval" })); });
    expect(await screen.findByText("Invoice line 1 (1000.00) does not fit PO line 2: 400.00 remaining, allowance 8.00.")).toBeInTheDocument();
  });

  it("a stale preview is replaced and explained", async () => {
    const fresh = { ...AUTO.approve, state_token: "fresh", po: { ...AUTO.approve.po!, balance_before: "5990.00", balance_after: "651.92" } };
    mockFetch(AUTO, { status: 409, body: { error: "stale", message: "The purchase order changed since this preview was shown.", preview: fresh } });
    render(<ReviewItemScreen id={1} />);
    const confirm = await screen.findByRole("button", { name: "Confirm approval" });
    await act(async () => { fireEvent.click(confirm); });
    expect(await screen.findByRole("status")).toHaveTextContent("changed since this preview");
    expect(screen.getByText(/5,990.00 USD/)).toBeInTheDocument();
  });

  it("a blocked item explains why and still offers reject", async () => {
    mockFetch(blocked);
    render(<ReviewItemScreen id={4} />);
    expect(await screen.findByText(/No purchase order was matched/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm approval" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reject…" })).toBeInTheDocument();
  });
});

describe("rejecting an item", () => {
  it("states that nothing is written and sends one confirmed request", async () => {
    mockFetch(AUTO, { status: 200, body: { status: "rejected" } });
    render(<ReviewItemScreen id={1} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reject…" }));
    expect(screen.getByText(/No ledger entry and no allocation will be written/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Reason/), { target: { value: "Not our order" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm rejection" })); });
    expect(calls.find((c) => c.url.endsWith("/reject"))!.body).toEqual({ confirm: true, reason: "Not our order" });
    expect(await screen.findByRole("heading", { name: "Rejected" })).toBeInTheDocument();
  });
});

describe("the PO page after an unassigned approval", () => {
  it("shows the consumed-not-assigned figure and leaves every line's remaining amount untouched", async () => {
    mockFetch(poUnassigned);
    render(<PODetailScreen id={90} />);
    const stat = (await screen.findByText("Consumed, not assigned to a line")).parentElement!;
    expect(stat).toHaveTextContent("1,105.00 USD");
    const rows = screen.getByRole("heading", { name: "Lines" }).closest("section")!.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("600.00");                        // remaining = the full line amount
    expect(screen.getByText("How the commits are allocated")).toBeInTheDocument();
    expect(screen.getAllByText("PO total (no specific line)")).toHaveLength(2);
  });
});
