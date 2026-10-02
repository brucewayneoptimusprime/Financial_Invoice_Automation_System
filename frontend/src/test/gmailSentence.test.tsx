import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { GmailImport } from "../components/GmailImport";
import statusLive from "./fixtures/gmail_status_live.json";
import statusFake from "./fixtures/gmail_status_fake.json";
import searchSentence from "./fixtures/gmail_search_sentence.json";
import searchOk from "./fixtures/gmail_search.json";
import translate422 from "./fixtures/gmail_translate_422.json";

// Plan 2, stage B2: the plain-English sentence box. Fixtures recorded from the real endpoints with a scripted model double.

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

const searches = () => calls.filter((c) => c.method === "POST" && c.url === "/api/gmail/search").map((c) => c.body);

async function find(text: string) {
  fireEvent.change(await screen.findByLabelText("Describe what you're looking for"), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Find" })); });
}

describe("the sentence box", () => {
  it("appears only when the translator is available, above the manual box", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusLive] });
    render(<GmailImport onImported={() => {}} />);
    expect(await screen.findByLabelText("Describe what you're looking for")).toHaveAttribute("maxLength", "300");
    expect(screen.getByLabelText("Gmail search")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find" })).toBeDisabled();             // nothing typed yet
  });

  it("is absent without the translator: the manual box alone, exactly as before", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake] });
    render(<GmailImport onImported={() => {}} />);
    await screen.findByLabelText("Gmail search");
    expect(screen.queryByLabelText("Describe what you're looking for")).toBeNull();
  });

  it("sends the sentence, puts Claude's query in the editable box, shows the notes and the cost, and ticks nothing", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusLive], "POST /api/gmail/search": () => [200, searchSentence] });
    render(<GmailImport onImported={() => {}} />);
    await find("invoices from SuperStore since September");
    expect(searches()).toEqual([{ sentence: "invoices from SuperStore since September" }]);
    expect(screen.getByLabelText("Gmail search")).toHaveValue("SuperStore after:2026/09/01");
    expect(screen.getByRole("status")).toHaveTextContent("Claude wrote this search");
    expect(screen.getByRole("status")).toHaveTextContent("Emails mentioning SuperStore since 1 September 2026.");
    expect(screen.getByText("SuperStore after:2026/09/01 has:attachment")).toBeInTheDocument();
    expect(screen.getByTestId("gmail-cost")).toHaveTextContent("This search: $0.0018 (query by Claude)");
    expect((screen.getAllByRole("checkbox") as HTMLInputElement[]).every((b) => !b.checked)).toBe(true);
  });

  it("the translated query stays editable: Search again sends the edited query, with no model involved", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusLive], "POST /api/gmail/search": (b) => [200, b.sentence ? searchSentence : searchOk] });
    render(<GmailImport onImported={() => {}} />);
    await find("invoices from SuperStore since September");
    fireEvent.change(screen.getByLabelText("Gmail search"), { target: { value: "SuperStore after:2026/08/01" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Search again" })); });
    expect(searches()[1]).toEqual({ query: "SuperStore after:2026/08/01" });
    expect(screen.queryByTestId("gmail-cost")).toBeNull();                            // a typed search costs nothing
    expect(screen.queryByText(/Claude wrote this search/)).toBeNull();
  });

  it("a refused translation puts Claude's query in the manual box with the problem, and shows what the attempt cost", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusLive], "POST /api/gmail/search": () => [422, translate422] });
    render(<GmailImport onImported={() => {}} />);
    await find("every SuperStore email anywhere");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Claude's search was not accepted; edit it in the Gmail search box.");
    expect(alert).toHaveTextContent("never looks in spam, trash or all mail");
    expect(alert).toHaveTextContent(/Cost of the attempt: \$0\.00/);
    expect(screen.getByLabelText("Gmail search")).toHaveValue("in:anywhere SuperStore");
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("an unavailable translator keeps whatever was in the manual box, which still works", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusLive],
              "POST /api/gmail/search": (b) => b.sentence
                ? [422, { error: "translation_failed", reason: "unavailable", query: null, notes: "", problems: [],
                          message: "Plain-English search is not available just now; use the Gmail search box.",
                          cost: { translate_usd: "0.000000", tokens_in: 0, tokens_out: 0 } }]
                : [200, searchOk] });
    render(<GmailImport onImported={() => {}} />);
    fireEvent.change(await screen.findByLabelText("Gmail search"), { target: { value: "after:2026/08/01" } });
    await find("invoices from SuperStore");
    expect(screen.getByRole("alert")).toHaveTextContent("use the Gmail search box");
    expect(screen.getByLabelText("Gmail search")).toHaveValue("after:2026/08/01");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Search" })); });
    expect(searches()[1]).toEqual({ query: "after:2026/08/01" });
    expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(0);
  });

  it("shows the sender text from a sentence search as plain text too", async () => {
    const hostile = JSON.parse(JSON.stringify(searchSentence));
    hostile.translation.notes = "<b>bold</b> notes";
    mockApi({ "GET /api/gmail/status": () => [200, statusLive], "POST /api/gmail/search": () => [200, hostile] });
    const { container } = render(<GmailImport onImported={() => {}} />);
    await find("x");
    expect(within(screen.getByRole("status")).getByText(/<b>bold<\/b> notes/)).toBeInTheDocument();
    expect(container.querySelector(".gmail-panel b")).toBeNull();
  });
});
