import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { UploadScreen } from "../screens/Upload";
import type { Health } from "../types";
import ss10963 from "./fixtures/ss_10963.view.json";
import matched from "./fixtures/po_detail_matched.json";

const health = { status: "ok", mode: "replay", model: "claude-sonnet-5", session_spent_usd: "0", session_ceiling_usd: "5.00",
                 run_ceiling_usd: "0.25", queue_length: 0, max_file_bytes: 20971520, max_files_per_upload: 3 } as Health;

let posts: string[] = [];
function mockFetch() {
  posts = [];
  let n = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/api/runs?")) return reply(200, { runs: [] });
    if (url === "/api/runs" && init?.method === "POST") {
      const name = (init.body as FormData).get("file") as File;
      posts.push(name.name);
      if (name.name.endsWith(".txt")) return reply(415, { error: "unsupported_type", message: `${name.name} looks like text; only PDF, PNG and JPEG are accepted.` });
      n += 1;
      return reply(202, { run_id: `run${n}`.padEnd(32, "0") });
    }
    if (url.startsWith("/api/runs/")) return reply(200, { ...ss10963, run: { ...ss10963.run, id: url.split("/")[3] } });
    if (url.startsWith("/api/pos/")) return reply(200, matched);
    return reply(404, {});
  }));
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); window.history.replaceState(null, "", "/"); });

const files = (...names: string[]) => names.map((n) => new File(["%PDF-1.4"], n, { type: n.endsWith(".txt") ? "text/plain" : "application/pdf" }));

describe("multi-file upload", () => {
  it("posts one request per file in order; a rejected file fails alone; each run ends with its own decision", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: files("a.pdf", "notes.txt", "c.pdf") } }); });
    await waitFor(() => expect(posts).toEqual(["a.pdf", "notes.txt", "c.pdf"]));
    const list = screen.getByRole("heading", { name: "This upload" }).parentElement!;
    const row = (name: string) => within(list).getByText(name).closest("li")!;
    expect(within(row("notes.txt")).getByText("not accepted")).toBeInTheDocument();
    expect(within(row("notes.txt")).getByText(/only PDF, PNG and JPEG/)).toBeInTheDocument();
    await waitFor(() => expect(within(row("a.pdf")).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    await waitFor(() => expect(within(row("c.pdf")).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    expect(within(row("a.pdf")).getByText("matched PO-SS-001")).toBeInTheDocument();
    expect(row("a.pdf").querySelector("a")!.getAttribute("href")).toMatch(/^\/runs\/run1/);
  });

  it("refuses more files than allowed, without uploading any", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: files("1.pdf", "2.pdf", "3.pdf", "4.pdf") } }); });
    expect(screen.getByRole("alert")).toHaveTextContent("At most 3 files per upload");
    expect(posts).toEqual([]);
  });

  it("a single file still goes straight to its live run view", async () => {
    mockFetch();
    const push = vi.spyOn(window.history, "pushState");
    render(<UploadScreen health={health} />);
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: files("one.pdf") } }); });
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/runs/${"run1".padEnd(32, "0")}`));
  });
});

describe("uploading from a PO page", () => {
  it("says matching is automatic and marks invoices that matched another PO", async () => {
    mockFetch();
    window.history.replaceState(null, "", "/?po=2");
    render(<UploadScreen health={health} />);
    expect(await screen.findByRole("note")).toHaveTextContent("Matching is automatic");
    await waitFor(() => expect(screen.getByRole("link", { name: "PO-SS-001" })).toBeInTheDocument());
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: files("x.pdf") } }); });
    // with a PO context even one file stays on this page, so the outcome can be compared with the PO
    const list = await screen.findByRole("heading", { name: "This upload" });
    await waitFor(() => expect(within(list.parentElement!).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    expect(posts).toEqual(["x.pdf"]);                                   // no PO hint is sent: the upload is the same request
  });
});
