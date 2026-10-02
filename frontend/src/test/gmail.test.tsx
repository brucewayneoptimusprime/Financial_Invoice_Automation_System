import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { GmailImport } from "../components/GmailImport";
import { RunScreen } from "../screens/Run";
import { UploadScreen } from "../screens/Upload";
import type { GmailSearchResult, Health } from "../types";
import statusFake from "./fixtures/gmail_status_fake.json";
import statusDisabled from "./fixtures/gmail_status_disabled.json";
import statusGoogle from "./fixtures/gmail_status_google.json";
import searchOk from "./fixtures/gmail_search.json";
import search422 from "./fixtures/gmail_search_422.json";
import importOk from "./fixtures/gmail_import.json";
import runView from "./fixtures/gmail_run_view.json";

// Fixtures recorded from the real backend endpoints with the labelled fake inbox (backend: python -m tests.gmail.frontend_fixtures).

const health = { status: "ok", mode: "replay", model: "claude-sonnet-5", session_spent_usd: "0", session_ceiling_usd: "5.00",
                 run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520, max_files_per_upload: 20 } as Health;

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
    if (handler) return reply(...handler(body));
    if (url.startsWith("/api/runs?")) return reply(200, { runs: [] });
    if (url.startsWith("/api/runs/")) return reply(200, { ...runView, run: { ...runView.run, id: url.split("/")[3] } });
    return reply(404, { error: "not_found", message: "no route" });
  }));
}

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

const posts = (path: string) => calls.filter((c) => c.method === "POST" && c.url === path);

async function searchFor(text: string) {
  const box = await screen.findByLabelText("Gmail search");
  fireEvent.change(box, { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^Search/ })); });
}

describe("the Gmail panel's states", () => {
  it("not set up: names the missing settings and offers no search", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusDisabled] });
    render(<GmailImport onImported={() => {}} />);
    expect(await screen.findByText(/missing GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_ENCRYPTION_KEY/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Gmail search")).toBeNull();
    expect(screen.queryByRole("button", { name: /Connect Gmail/ })).toBeNull();
  });

  it("not connected: Connect starts the OAuth flow and leaves for Google's URL", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusGoogle],
              "POST /api/gmail/oauth/start": () => [200, { authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" }] });
    const go = vi.fn();
    render(<GmailImport onImported={() => {}} navigateTo={go} hostname="localhost" />);
    const connect = await screen.findByRole("button", { name: "Connect Gmail (read-only)" });
    await act(async () => { fireEvent.click(connect); });
    expect(go).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?x=1");
    expect(screen.getByText(/read-only: nothing is sent, changed or deleted/)).toBeInTheDocument();
  });

  it("on 127.0.0.1 it warns to open localhost:5173 and will not start a connection", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusGoogle] });
    render(<GmailImport onImported={() => {}} hostname="127.0.0.1" navigateTo={vi.fn()} />);
    const warning = await screen.findByRole("alert");
    expect(warning).toHaveTextContent("Open this page at http://localhost:5173");
    expect(within(warning).getByRole("link")).toHaveAttribute("href", "http://localhost:5173/invoices");
    expect(await screen.findByRole("button", { name: "Connect Gmail (read-only)" })).toBeDisabled();
  });

  it("a stored connection that cannot be used asks to connect again", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, { ...statusGoogle, reconnect: true }] });
    render(<GmailImport onImported={() => {}} hostname="localhost" />);
    expect(await screen.findByText(/can no longer be used. Connect again/)).toBeInTheDocument();
  });

  it("shows the result of returning from Google and cleans the address bar", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusGoogle] });
    window.history.replaceState(null, "", "/invoices?gmail=error&code=state_expired&po=3");
    render(<GmailImport onImported={() => {}} />);
    expect(await screen.findByRole("status")).toHaveTextContent("The sign-in took too long and expired");
    expect(window.location.search).toBe("?po=3");
  });

  it("an unexpected status answer never breaks the upload screen", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, {}] });
    render(<UploadScreen health={health} />);
    expect(await screen.findByText("Gmail import is not available from this server.")).toBeInTheDocument();
    expect(screen.getByText("Drop invoices here")).toBeInTheDocument();
  });

  it("connected (fake): only the plain Gmail search box until the translator exists", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake] });
    render(<GmailImport onImported={() => {}} />);
    expect(await screen.findByLabelText("Gmail search")).toHaveAttribute("maxLength", "300");
    expect(screen.getByText("FAKE INBOX (test data)")).toBeInTheDocument();
    expect(screen.getByText(/fake-inbox@example.test · read-only/)).toBeInTheDocument();
    expect(screen.queryAllByRole("textbox").concat(screen.queryAllByRole("searchbox"))).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();          // nothing to disconnect in the fake inbox
  });
});

describe("searching", () => {
  it("sends the query as typed, shows what was sent, and pre-ticks nothing", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [200, searchOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchFor("after:2026/08/01");
    expect(posts("/api/gmail/search")[0].body).toEqual({ query: "after:2026/08/01" });
    expect(await screen.findByText("after:2026/08/01 has:attachment")).toBeInTheDocument();
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.length).toBeGreaterThan(5);
    expect(boxes.every((b) => !b.checked)).toBe(true);
    expect(screen.getByRole("button", { name: "Import 0 selected" })).toBeDisabled();
    expect(screen.getByLabelText("Gmail search")).toHaveValue("after:2026/08/01");   // still editable for the next search
    expect(screen.getByRole("button", { name: "Search again" })).toBeInTheDocument();
  });

  it("shows the validator's refusal with every problem and no results", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [422, search422] });
    render(<GmailImport onImported={() => {}} />);
    await searchFor("in:anywhere label:finance invoice");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("never looks in spam, trash or all mail");
    expect(within(alert).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("shows sender, subject and snippet as plain text, never as HTML", async () => {
    const hostile = JSON.parse(JSON.stringify(searchOk)) as GmailSearchResult;
    hostile.messages[0].snippet = '<img src=x onerror="alert(1)"><b>bold</b>';
    hostile.messages[0].subject = "<script>alert(2)</script>";
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [200, hostile] });
    const { container } = render(<GmailImport onImported={() => {}} />);
    await searchFor("x");
    expect(await screen.findByText('<img src=x onerror="alert(1)"><b>bold</b>')).toBeInTheDocument();
    expect(screen.getByText("<script>alert(2)</script>")).toBeInTheDocument();
    expect(container.querySelector(".gmail-panel img, .gmail-panel script, .gmail-panel b")).toBeNull();
  });

  it("marks text addressed to an AI, greys ineligible files with the reason, and links what was imported", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [200, searchOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchFor("x");
    expect(await screen.findByText(/in the subject, snippet/)).toBeInTheDocument();
    expect(screen.getByLabelText("Import invoices_q3.zip")).toBeDisabled();
    expect(screen.getByText("ZIP and other archive files are not imported.")).toBeInTheDocument();
    expect(screen.getByLabelText("Import scan_large.pdf")).toBeDisabled();
    expect(screen.getByLabelText("Import invoice_Scot Wooten_10963.pdf")).toBeDisabled();       // imported earlier
    expect(screen.getByRole("link", { name: "imported, see run" }).getAttribute("href")).toMatch(/^\/runs\//);
    expect(screen.getByText(/inline image/)).toBeInTheDocument();
  });

  it("stops ticking at the per-import cap", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, { ...statusFake, caps: { ...statusFake.caps, max_import: 2 } }],
              "POST /api/gmail/search": () => [200, searchOk] });
    render(<GmailImport onImported={() => {}} />);
    await searchFor("x");
    const pick = (name: string) => screen.getByLabelText(`Import ${name}`) as HTMLInputElement;
    fireEvent.click(await screen.findByLabelText("Import invoice_Bill Eplett_14021.pdf"));
    fireEvent.click(pick("invoice_Liz Thompson_14130.pdf"));
    expect(pick("invoice_Maria Zettner_24429.pdf")).toBeDisabled();
    expect(screen.getByText(/Limit reached/)).toBeInTheDocument();
    fireEvent.click(pick("invoice_Liz Thompson_14130.pdf"));                              // untick one: the others open again
    expect(pick("invoice_Maria Zettner_24429.pdf")).not.toBeDisabled();
  });
});

describe("importing", () => {
  it("imports exactly the ticked attachments and the runs join 'This upload'", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [200, searchOk],
              "POST /api/gmail/import": () => [200, importOk] });
    render(<UploadScreen health={health} />);
    await searchFor("after:2026/08/01");
    fireEvent.click(await screen.findByLabelText("Import invoice_Bill Eplett_14021.pdf"));
    fireEvent.click(screen.getByLabelText("Import acme_inv_7781.pdf"));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Import 2 selected" })); });
    expect(posts("/api/gmail/import")[0].body).toEqual({
      search_id: searchOk.search_id, confirm: true,
      items: [{ message_id: "fake-ss-two", part_id: "1" }, { message_id: "fake-acme", part_id: "2" }] });
    const list = (await screen.findByRole("heading", { name: "This upload" })).parentElement!;
    const row = (name: string) => within(list).getAllByText(name)[0].closest("li")!;
    expect(within(row("acme_inv_7781.pdf")).getByText("not accepted")).toBeInTheDocument();
    expect(within(row("invoice_Scot Wooten_10963.pdf")).getByText(/already imported: the earlier run/)).toBeInTheDocument();
    await waitFor(() => expect(within(row("invoice_Bill Eplett_14021.pdf")).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    expect(within(row("invoice_Bill Eplett_14021.pdf")).getByText(/from Gmail/)).toBeInTheDocument();
    expect(posts("/api/gmail/search")).toHaveLength(2);                                  // refreshed after the import
  });

  it("shows a refused import (budget) and imports nothing", async () => {
    const onImported = vi.fn();
    mockApi({ "GET /api/gmail/status": () => [200, statusFake], "POST /api/gmail/search": () => [200, searchOk],
              "POST /api/gmail/import": () => [409, { error: "budget", message: "The model budget left for this server session ($0.30) covers at most 1 import at the $0.25 per-run ceiling. Pick 1 or fewer.", fits: 1 }] });
    render(<GmailImport onImported={onImported} />);
    await searchFor("x");
    fireEvent.click(await screen.findByLabelText("Import invoice_Bill Eplett_14021.pdf"));
    fireEvent.click(screen.getByLabelText("Import invoice_Liz Thompson_14130.pdf"));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Import 2 selected" })); });
    expect(await screen.findByRole("alert")).toHaveTextContent("covers at most 1 import");
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.getByText(/Budget left this session: \$5\.00/)).toBeInTheDocument();
  });

  it("disconnect asks first and then calls the server", async () => {
    mockApi({ "GET /api/gmail/status": () => [200, { ...statusGoogle, connected: true, account_email: "inbox@example.com" }],
              "POST /api/gmail/disconnect": () => [200, { disconnected: true, revoked: true }] });
    render(<GmailImport onImported={() => {}} hostname="localhost" />);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(posts("/api/gmail/disconnect")).toHaveLength(0);
    expect(screen.getByText("Disconnect this account?")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Disconnect" })); });
    expect(posts("/api/gmail/disconnect")[0].body).toEqual({ confirm: true });
    expect(await screen.findByRole("alert")).toHaveTextContent("Gmail disconnected.");
  });
});

describe("the run page", () => {
  it("says the file came from Gmail, with the sender and date as plain text", async () => {
    vi.stubGlobal("EventSource", class {
      onopen = null; onerror = null; readyState = 0; static CLOSED = 2;
      addEventListener(type: string, fn: (m: { data: string }) => void) {
        if (type === "end") setTimeout(() => fn({ data: JSON.stringify({ status: "completed", decision: "review" }) }), 0);
      }
      close() {}
    });
    mockApi({});
    render(<RunScreen runId={runView.run.id} />);
    expect(await screen.findByTestId("gmail-source")).toHaveTextContent("From Gmail: SuperStore Billing <billing@superstore.example>");
  });
});
