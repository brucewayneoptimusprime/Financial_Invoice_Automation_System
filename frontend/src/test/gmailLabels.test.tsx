import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { GmailImport } from "../components/GmailImport";
import type { GmailSearchResult } from "../types";
import statusLive from "./fixtures/gmail_status_live.json";
import searchSentence from "./fixtures/gmail_search_sentence.json";
import labelsOk from "./fixtures/gmail_labels.json";
import importOk from "./fixtures/gmail_import.json";

// Plan 2, stage C2: advisory relevance labels in the panel. Fixtures recorded from the real endpoints with a scripted model double.

interface Call { url: string; method: string; body: any }
let calls: Call[] = [];

function mockApi(routes: Record<string, (body: any) => [number, unknown]>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const handler = routes[`${method} ${url.split("?")[0]}`];
    const reply = (status: number, b: unknown) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
    return handler ? reply(...handler(body)) : reply(404, { error: "not_found", message: "no route" });
  }));
}
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

const posts = (path: string) => calls.filter((c) => c.method === "POST" && c.url === path).map((c) => c.body);

async function searchQuery(text = "SuperStore after:2026/09/01") {
  const toggle = await screen.findByRole("button", { name: "Edit search query" });      // the query box is collapsed by default
  if (toggle.getAttribute("aria-expanded") === "false") fireEvent.click(toggle);
  fireEvent.change(await screen.findByLabelText("Gmail search"), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^Search/ })); });
}

const base = { "GET /api/gmail/status": () => [200, statusLive] as [number, unknown],
               "POST /api/gmail/search": () => [200, searchSentence] as [number, unknown] };

describe("relevance labels", () => {
  it("asks once per search and shows each label with its reason next to the right checkbox", async () => {
    mockApi({ ...base, "POST /api/gmail/labels": () => [200, labelsOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(posts("/api/gmail/labels")).toEqual([{ search_id: searchSentence.search_id }]);
    const hint = await screen.findByTestId("label-fake-ss-two|1");
    expect(hint).toHaveTextContent("likely invoice");
    expect(hint).toHaveTextContent("PDF named like a SuperStore invoice");
    expect(screen.getByTestId("label-fake-ss-10963|1")).toHaveTextContent("unsure");
    expect(screen.getByTestId("label-fake-injection|1")).toHaveTextContent("The email contains text addressed to an AI");
    expect(screen.getByText(/Labels are hints from Claude, read from the email's details only. Nothing is ticked for you./)).toBeInTheDocument();
  });

  it("labels change nothing: same order, same checkboxes, nothing ticked, and the same import request", async () => {
    const snapshot = async (withLabels: boolean) => {
      mockApi({ ...base, "POST /api/gmail/labels": () => withLabels ? [200, labelsOk] : [404, { error: "x" }],
                "POST /api/gmail/import": () => [200, importOk] });
      const { unmount } = render(<GmailImport onImported={() => {}} />);
      await searchQuery();
      if (withLabels) await screen.findByTestId("label-fake-ss-two|1");
      const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
      const state = boxes.map((b) => [b.getAttribute("aria-label"), b.checked, b.disabled]);
      const order = Array.from(document.querySelectorAll(".gmail-msg .gmail-from, .gmail-msg .gmail-file")).map((e) => e.textContent);
      fireEvent.click(screen.getByLabelText("Import invoice_Liz Thompson_14130.pdf"));
      fireEvent.click(screen.getByLabelText("Import invoice_Scot Wooten_10963.pdf"));       // labelled "unsure": still tickable
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Import 2 selected" })); });
      const sent = posts("/api/gmail/import")[0];
      unmount();
      vi.unstubAllGlobals();
      return { state, order, sent };
    };
    const withL = await snapshot(true);
    const without = await snapshot(false);
    expect(withL.state).toEqual(without.state);
    expect(withL.state.every(([, checked]) => checked === false)).toBe(true);
    expect(withL.order).toEqual(without.order);
    expect(withL.sent).toEqual(without.sent);
  });

  it("shows the cost of the query and the labels together", async () => {
    mockApi({ ...base, "POST /api/gmail/labels": () => [200, labelsOk] });
    render(<GmailImport onImported={() => {}} />);
    fireEvent.change(await screen.findByLabelText("Describe what you're looking for"), { target: { value: "invoices from SuperStore since September" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Find" })); });
    await screen.findByTestId("label-fake-ss-two|1");
    expect(screen.getByTestId("gmail-cost")).toHaveTextContent("This search: $0.0018 (query) + $0.0080 (labels) = $0.0098, by Claude.");
  });

  it("makes no labels call when no attachment can be imported", async () => {
    const zipOnly = JSON.parse(JSON.stringify(searchSentence)) as GmailSearchResult;
    zipOnly.messages = zipOnly.messages.filter((m) => m.attachments.every((a) => !a.eligible));
    expect(zipOnly.messages.length).toBeGreaterThan(0);
    mockApi({ ...base, "POST /api/gmail/search": () => [200, zipOnly], "POST /api/gmail/labels": () => [200, labelsOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(posts("/api/gmail/labels")).toEqual([]);
    expect(screen.queryByTestId("gmail-cost")).toBeNull();
  });

  it("makes no labels call when the server has no labeller", async () => {
    mockApi({ ...base, "GET /api/gmail/status": () => [200, { ...statusLive, labels_available: false }],
              "POST /api/gmail/labels": () => [200, labelsOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(posts("/api/gmail/labels")).toEqual([]);
  });

  it("says so when no labels came back, and the list still works", async () => {
    mockApi({ ...base, "POST /api/gmail/labels": () => [200, { ...labelsOk, labels: [], fallback: "invalid_output" }] });
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(await screen.findByText("No labels for this search (Claude was not available).")).toBeInTheDocument();
    expect(screen.queryByTestId("label-fake-ss-two|1")).toBeNull();
    expect(screen.getByLabelText("Import invoice_Bill Eplett_14021.pdf")).not.toBeDisabled();
  });

  it("shows a reason as plain text, never HTML", async () => {
    const hostile = JSON.parse(JSON.stringify(labelsOk));
    hostile.labels[1].reason = '<img src=x onerror="alert(1)"> invoice';
    mockApi({ ...base, "POST /api/gmail/labels": () => [200, hostile] });
    const { container } = render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(await screen.findByText('<img src=x onerror="alert(1)"> invoice')).toBeInTheDocument();
    expect(container.querySelector(".gmail-panel img")).toBeNull();
  });

  it("shows 'Labelling…' while the labels are on their way", async () => {
    let release: (v: Response) => void = () => {};
    calls = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const ok = (b: unknown) => Promise.resolve(new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } }));
      if (url === "/api/gmail/status") return ok(statusLive);
      if (url === "/api/gmail/search") return ok(searchSentence);
      if (url === "/api/gmail/labels") return new Promise<Response>((r) => { release = r; });
      return ok({});
    }));
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    expect(screen.getByText("Labelling the attachments…")).toBeInTheDocument();
    await act(async () => { release(new Response(JSON.stringify(labelsOk), { status: 200, headers: { "Content-Type": "application/json" } })); });
    expect(await screen.findByTestId("label-fake-ss-two|1")).toBeInTheDocument();
    expect(screen.queryByText("Labelling the attachments…")).toBeNull();
  });

  it("the refresh after an import keeps the labels and does not ask again", async () => {
    mockApi({ ...base, "POST /api/gmail/labels": () => [200, labelsOk], "POST /api/gmail/import": () => [200, importOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchQuery();
    await screen.findByTestId("label-fake-ss-two|1");
    fireEvent.click(screen.getByLabelText("Import invoice_Liz Thompson_14130.pdf"));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Import 1 selected" })); });
    expect(posts("/api/gmail/search")).toHaveLength(2);
    expect(posts("/api/gmail/labels")).toHaveLength(1);
    expect(within(screen.getByTestId("label-fake-ss-two|1")).getByText("likely invoice")).toBeInTheDocument();
  });
});
