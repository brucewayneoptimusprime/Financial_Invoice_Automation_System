import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { CrossCheck } from "../components/CrossCheck";
import { PODetailScreen } from "../screens/PODetail";
import infoLive from "./fixtures/crosscheck_info.json";
import infoOffline from "./fixtures/crosscheck_info_offline.json";
import report from "./fixtures/crosscheck_report.json";
import budget from "./fixtures/crosscheck_budget.json";
import matched from "./fixtures/po_detail_matched.json";

// Cross-check documents (CROSSCHECK_PLAN stage C4). The fixtures are recorded from the real endpoints by
// backend/tests/crosscheck/frontend_fixtures.py. Report only: the label is always shown, nothing is sent before Analyze, and the
// report has no verdict.

const LABEL = "Report only: nothing here changes the PO, its invoices, the ledger or any decision.";
let calls: { url: string; init?: RequestInit }[] = [];

function mockFetch(opts: { info?: unknown; infoStatus?: number; post?: unknown; postStatus?: number } = {}) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (url.endsWith("/crosscheck")) {
      if (init?.method === "POST") return reply(opts.post ?? report, opts.postStatus ?? 200);
      return reply(opts.info ?? infoLive, opts.infoStatus ?? 200);
    }
    return reply(url.startsWith("/api/pos/") ? matched : {});
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const file = (name: string, size = 2048, type = "application/pdf") => new File([new Uint8Array(size)], name, { type });
const posts = () => calls.filter((c) => c.init?.method === "POST");

async function section(opts: Parameters<typeof mockFetch>[0] = {}) {
  mockFetch(opts);
  render(<CrossCheck poId={7} />);
  return screen.findByRole("region", { name: "Cross-check documents (report only)" });
}

function choose(...files: File[]) {
  fireEvent.change(screen.getByLabelText("Documents to cross-check"), { target: { files } });
}

async function analysed(opts: Parameters<typeof mockFetch>[0] = {}, ...files: File[]) {
  await section(opts);
  choose(...(files.length ? files : [file("delivery-note.pdf")]));
  fireEvent.click(screen.getByRole("button", { name: /^Analyze/ }));
  await waitFor(() => expect(posts()).toHaveLength(1));
}

describe("the section before Analyze", () => {
  it("always shows the report-only label, the limits and the cost before, and sends nothing", async () => {
    const s = await section();
    expect(within(s).getByTestId("cc-label")).toHaveTextContent(LABEL);
    expect(s).toHaveTextContent("Up to 5 files · PDF, PNG, JPG · at most 20 MB each");
    expect(within(s).getByTestId("cc-before")).toHaveTextContent(
      "Estimated cost: about $0.02 per document. Never more than $0.25 per document. Budget left this session: $5.00.");
    expect(within(s).getByRole("button", { name: "Analyze" })).toBeDisabled();
    expect(calls.map((c) => c.url)).toEqual(["/api/pos/7/crosscheck"]);
    expect(posts()).toHaveLength(0);
  });

  it("stages chosen files without uploading them, shows the cost for that many, and lets one be removed", async () => {
    const s = await section();
    choose(file("delivery-note.pdf"), file("goods-receipt.pdf"), file("scan.JPG", 4096, "image/jpeg"));
    const list = within(s).getByRole("list", { name: "Documents to analyse" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(s).getByTestId("cc-before")).toHaveTextContent("about $0.02 per document, so about $0.06 for 3 documents");
    expect(within(s).getByRole("button", { name: "Analyze 3 documents" })).toBeEnabled();
    fireEvent.click(within(s).getByRole("button", { name: "Remove goods-receipt.pdf" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(s).getByTestId("cc-before")).toHaveTextContent("so about $0.04 for 2 documents");
    expect(posts()).toHaveLength(0);                                   // nothing has been sent
  });

  it("keeps at most five files and refuses the wrong type, an empty file and an oversize one with a reason", async () => {
    const s = await section();
    choose(...[1, 2, 3, 4, 5, 6].map((i) => file(`note-${i}.pdf`)), file("sheet.xlsx"), file("empty.pdf", 0));
    expect(within(within(s).getByRole("list", { name: "Documents to analyse" })).getAllByRole("listitem")).toHaveLength(5);
    const alert = within(s).getByRole("alert");
    expect(alert).toHaveTextContent("note-6.pdf was left out: at most 5 documents can be analysed at once");
    expect(alert).toHaveTextContent("sheet.xlsx is not a PDF, PNG or JPG file");
    expect(alert).toHaveTextContent("empty.pdf is empty");
    expect(within(s).getByRole("button", { name: "Choose documents" })).toBeDisabled();
    const big = file("big.pdf", 1);
    Object.defineProperty(big, "size", { value: 21 * 1_048_576 });
    fireEvent.click(within(s).getByRole("button", { name: "Clear" }));
    choose(big);
    expect(within(s).getByRole("alert")).toHaveTextContent("big.pdf is larger than 20 MB");
  });

  it("offline: says so, and nothing can be chosen or analysed", async () => {
    const s = await section({ info: infoOffline });
    expect(within(s).getByRole("status")).toHaveTextContent("Offline mode: no model is available to read documents.");
    expect(within(s).getByRole("button", { name: "Choose documents" })).toBeDisabled();
    expect(within(s).getByRole("button", { name: "Analyze" })).toBeDisabled();
    expect(within(s).getByTestId("cc-label")).toHaveTextContent(LABEL);
  });

  it("replay: the note is shown and analysing stays possible", async () => {
    const s = await section({ info: { ...infoLive, mode: "replay", message: "Replay mode: only a document with a recorded answer can be analysed." } });
    expect(within(s).getByRole("status")).toHaveTextContent("Replay mode: only a document with a recorded answer can be analysed.");
    expect(within(s).getByRole("button", { name: "Choose documents" })).toBeEnabled();
  });

  it("switched off or unknown answer: there is no section at all", async () => {
    mockFetch({ info: { error: "not_found", message: "Cross-check is switched off." }, infoStatus: 404 });
    const { container, unmount } = render(<CrossCheck poId={7} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(container).toBeEmptyDOMElement();
    unmount();
    mockFetch({ info: matched });                                      // some other JSON: not a cross-check answer
    const again = render(<CrossCheck poId={7} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(again.container).toBeEmptyDOMElement();
  });
});

describe("Analyze and the report", () => {
  it("sends the chosen files in one request only when Analyze is clicked, and shows the cost after", async () => {
    await analysed({}, file("delivery-note.pdf"), file("goods-receipt.pdf"));
    const [call] = posts();
    expect(call.url).toBe("/api/pos/7/crosscheck");
    const sent = (call.init!.body as FormData).getAll("files") as File[];
    expect(sent.map((f) => f.name)).toEqual(["delivery-note.pdf", "goods-receipt.pdf"]);
    const after = await screen.findByTestId("cc-after");
    expect(after).toHaveTextContent("This analysis cost $0.0420 (3 of 5 documents analysed, by Claude).");
    expect(after).toHaveTextContent(LABEL);
    expect(screen.getByTestId("cc-label")).toHaveTextContent(LABEL);
  });

  it("a matching document is Related, with its four reasons and No differences found", async () => {
    await analysed();
    const doc = await screen.findByRole("article", { name: "delivery-note.pdf" });
    expect(within(doc).getByText("Related")).toBeInTheDocument();
    expect(within(doc).getByText("Why it is considered related to this PO")).toBeInTheDocument();
    expect(within(doc).getByText("PO number: yes")).toBeInTheDocument();
    expect(within(doc).getByText("Invoice number: no")).toBeInTheDocument();
    expect(within(doc).getByText("Vendor: yes")).toBeInTheDocument();
    expect(within(doc).getByText("Lines: yes")).toBeInTheDocument();
    expect(doc).toHaveTextContent("The document mentions this PO's number. Document: PO-7001 · This PO: PO-7001");
    expect(within(doc).getByRole("heading", { name: "No differences found" })).toBeInTheDocument();
    expect(within(doc).queryByRole("table", { name: /differences/i })).toBeNull();
    expect(doc).toHaveTextContent("Quantity on PO line 1 against invoices: no invoice is matched to this PO.");
  });

  it("differences show both values and where each comes from", async () => {
    await analysed();
    const doc = await screen.findByRole("article", { name: "goods-receipt.pdf" });
    expect(within(doc).getByRole("heading", { name: "Differences found (4)" })).toBeInTheDocument();
    const rows = within(within(doc).getAllByRole("table")[0]).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getByRole("rowheader").textContent)).toEqual([
      "Item not on the PO", "Quantity, against the PO linePO line 1: Widget A", "Unit pricePO line 1: Widget A", "AmountPO line 1: Widget A"]);
    expect(rows[1]).toHaveTextContent("8p. 1 Widget A 8 pcs 62.50 500.00");
    expect(rows[1]).toHaveTextContent("10PO line 1, ordered quantity");
    expect(rows[2]).toHaveTextContent("62.50");
    expect(rows[2]).toHaveTextContent("60.00PO line 1, unit price");
    expect(rows[3]).toHaveTextContent("480.00Expected for 8 at the PO price 60.00 (PO line 1)");
    expect(doc).toHaveTextContent("This document contains text addressed to an AI reader. It was treated as data and changed nothing.");
    expect(doc).toHaveTextContent("Document total: not every line of the document is tied to a PO line.");
    expect(doc).toHaveTextContent('Could not be confirmed in the document text (not compared)');
    expect(doc).toHaveTextContent('Line 2 "Widget B": the value does not agree with the text quoted for it.');
    expect(doc).toHaveTextContent("PO lines not on this document (for information)");
  });

  it("an unrelated document is Not related with its reasons and no differences block", async () => {
    await analysed();
    const doc = await screen.findByRole("article", { name: "other-vendor.pdf" });
    expect(within(doc).getByText("Not related")).toBeInTheDocument();
    expect(within(doc).getByText("Why it is not considered related to this PO")).toBeInTheDocument();
    expect(within(doc).getByText("Vendor: no")).toBeInTheDocument();
    expect(within(doc).queryByRole("heading", { name: /differences/i })).toBeNull();
    expect(doc).toHaveTextContent("Document: PO-2001 · This PO: PO-7001");
  });

  it("a failed document shows its own message, including the replay one", async () => {
    await analysed();
    const empty = await screen.findByRole("article", { name: "empty.pdf" });
    expect(within(empty).getByText("Not analysed")).toBeInTheDocument();
    expect(within(empty).getByRole("status")).toHaveTextContent("empty.pdf is empty (0 bytes).");
    const replay = screen.getByRole("article", { name: "unrecorded.pdf" });
    expect(within(replay).getByRole("status")).toHaveTextContent("Replay mode: no recorded answer exists for this document.");
  });

  it("the report carries no verdict, score, severity or recommendation", async () => {
    await analysed();
    const s = await screen.findByRole("region", { name: "Cross-check documents (report only)" });
    await screen.findByTestId("cc-after");
    expect(s.textContent).not.toMatch(/\b(pass(ed)?|fail(ed)?|severity|recommend|approve|reject|score|risk)\b/i);
    expect(s.querySelectorAll(".chip-pass, .chip-fail, .chip-flag")).toHaveLength(0);
    expect(within(s).queryByRole("button", { name: /approve|reject|save|apply/i })).toBeNull();
  });

  it("a refused analysis shows the server's message and keeps the chosen files", async () => {
    await analysed({ post: budget, postStatus: 409 }, file("a.pdf"), file("b.pdf"));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not cover this analysis");
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was sent.");
    expect(within(screen.getByRole("list", { name: "Documents to analyse" })).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByTestId("cc-after")).toBeNull();
  });
});

describe("on the PO page", () => {
  it("is the last section of the page", async () => {
    mockFetch();
    render(<PODetailScreen id={7} />);
    const s = await screen.findByRole("region", { name: "Cross-check documents (report only)" });
    const sections = Array.from(document.querySelectorAll("section.section"));
    expect(sections[sections.length - 1]).toBe(s);
    expect(posts()).toHaveLength(0);
  });
});
