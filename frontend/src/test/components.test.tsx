import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { DecisionBanner, FieldsTable, ResultView } from "../components/Result";
import { StageTimeline } from "../components/StageTimeline";
import { initialRunState, runReducer } from "../runState";
import { money, stageLine } from "../format";
import { auditOf, STREAMS, VIEWS } from "./fixtures";

function finished(frames = STREAMS.ss10963) {
  let s = initialRunState();
  for (const e of auditOf(frames)) s = runReducer(s, { type: "audit", event: e, now: 0 });
  return s;
}

describe("format", () => {
  it("formats decimal strings without floats", () => {
    expect(money("1770.61", "USD")).toBe("1,770.61 USD");
    expect(money("5338.08")).toBe("5,338.08");
    expect(money("-12.5")).toBe("-12.50");
    expect(money("100000000.01")).toBe("100,000,000.01");
    expect(money(null)).toBe("—");
  });
  it("summarises stages from their summaries", () => {
    expect(stageLine("validate", { counts: { pass: 14, flag: 1, fail: 0 } })).toBe("14 pass · 1 flag · 0 fail");
    expect(stageLine("match", { vendor: "SuperStore", matched_po: "PO-SS-001", top_score: 0.576 })).toBe("SuperStore → PO-SS-001 (0.58)");
    expect(stageLine("extract", { degraded: true, failure_code: "config" })).toBe("Degraded: Config");
  });
});

describe("StageTimeline", () => {
  it("shows all seven stages with status, time and one line each", () => {
    render(<StageTimeline state={finished()} />);
    const items = screen.getAllByRole("listitem").filter((li) => li.classList.contains("stage"));
    expect(items).toHaveLength(7);
    expect(within(items[3]).getByText("Validate")).toBeInTheDocument();
    expect(within(items[3]).getByText("Flagged")).toBeInTheDocument();
    expect(within(items[3]).getByText("14 pass · 1 flag · 0 fail")).toBeInTheDocument();
    expect(within(items[2]).getByText(/SuperStore → PO-SS-001/)).toBeInTheDocument();
  });
  it("expands a stage into its events", () => {
    render(<StageTimeline state={finished()} />);
    fireEvent.click(screen.getByRole("button", { name: /Match/ }));
    expect(screen.getByText(/5 candidate purchase order\(s\) ranked/)).toBeInTheDocument();
  });
  it("marks a running stage as the current step", () => {
    const events = auditOf(STREAMS.ss10963);
    let s = initialRunState();
    for (const e of events.slice(0, 7)) s = runReducer(s, { type: "audit", event: e, now: Date.now() });
    render(<StageTimeline state={s} />);
    const current = document.querySelector('[aria-current="step"]');
    expect(current).not.toBeNull();
    expect(within(current as HTMLElement).getByText("Extract")).toBeInTheDocument();
    expect(within(current as HTMLElement).getByText("Running")).toBeInTheDocument();
  });
});

describe("DecisionBanner", () => {
  it("states each decision in words, not colour alone", () => {
    const { rerender } = render(<DecisionBanner view={VIEWS.ss10963} />);
    expect(screen.getByText("Review")).toBeInTheDocument();
    expect(screen.getByText(/Next step:/)).toBeInTheDocument();
    rerender(<DecisionBanner view={VIEWS.approve} />);
    expect(screen.getByText("Approve")).toBeInTheDocument();
    rerender(<DecisionBanner view={VIEWS.requestInfo} />);
    expect(screen.getByText("Request info")).toBeInTheDocument();
    rerender(<DecisionBanner view={VIEWS.failed} />);
    expect(screen.getByText("No decision")).toBeInTheDocument();
    expect(screen.getByText(/rolled back/)).toBeInTheDocument();
  });
  it("says where the explanation came from", () => {
    render(<DecisionBanner view={VIEWS.iq} />);
    expect(screen.getByText(/template/)).toBeInTheDocument();
  });
});

describe("FieldsTable", () => {
  it("shows value, confidence (with the capped model score), evidence status and source text", () => {
    const open = vi.fn();
    render(<FieldsTable view={VIEWS.iq} onOpenPage={open} />);
    const vendor = screen.getByRole("row", { name: /^Vendor IQ/ });
    expect(within(vendor).getByText("Mismatch")).toBeInTheDocument();
    expect(within(vendor).getByText("30%")).toBeInTheDocument();
    expect(within(vendor).getByText("model 75%")).toBeInTheDocument();
    const currency = screen.getByRole("row", { name: /^Currency/ });
    expect(within(currency).getByText("INR")).toBeInTheDocument();
    fireEvent.click(within(currency).getByRole("button", { name: /Show the page|Rupees/ }));
    expect(open).toHaveBeenCalledWith({ page: 1, label: "Currency", sourceText: "Rupees Four Thousand Nine Hundred only" });
    expect(within(screen.getByRole("row", { name: /^PO reference/ })).getByText("not found")).toBeInTheDocument();
  });
});

describe("ResultView", () => {
  it("synthetic approve variant: the ledger commit and the balance before and after", () => {
    render(<ResultView view={VIEWS.approve} />);
    expect(screen.getByText("PO-SS-002", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("2,500.00 USD")).toBeInTheDocument();
    expect(screen.getByText("729.39 USD")).toBeInTheDocument();
    expect(screen.getByText("Ready for payment")).toBeInTheDocument();
  });
  it("synthetic request_info variant: the draft is shown and marked as not sent", () => {
    render(<ResultView view={VIEWS.requestInfo} />);
    expect(screen.getByText("Vendor email (draft)")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent/)).toBeInTheDocument();
    expect(screen.getByText(/no vendor contact on file/)).toBeInTheDocument();
  });
  it("lists triggered checks first", () => {
    render(<ResultView view={VIEWS.iq} />);
    const names = [...document.querySelectorAll(".rule .rule-name")].map((n) => n.textContent);
    expect(names.slice(0, 3).join(" ")).toMatch(/Engine floor|confidence|purchase order/i);
    expect(document.querySelectorAll(".rule")).toHaveLength(15);
  });
  it("opens the page viewer from the evidence and closes it with Escape", () => {
    render(<ResultView view={VIEWS.iq} />);
    fireEvent.click(screen.getByRole("button", { name: "View invoice" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveAttribute("src", `/api/runs/${VIEWS.iq.run.id}/pages/1`);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
