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
      if (name.name.endsWith(".txt") || name.name.startsWith("fake")) return reply(415, { error: "unsupported_type", message: `${name.name} looks like text; only PDF, PNG and JPEG are accepted.` });
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

const choose = async (...names: string[]) => {
  await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: files(...names) } }); });
};
const processBtn = () => screen.getByRole("button", { name: /^Process \d+ invoices?$/ });
const process = async () => { await act(async () => { fireEvent.click(processBtn()); }); };

describe("multi-file upload", () => {
  it("posts one request per file in order; a rejected file fails alone; each run ends with its own decision", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await choose("a.pdf", "fake.pdf", "c.pdf");
    await process();
    await waitFor(() => expect(posts).toEqual(["a.pdf", "fake.pdf", "c.pdf"]));
    const list = screen.getByRole("heading", { name: "This upload" }).parentElement!;
    const row = (name: string) => within(list).getByText(name).closest("li")!;
    expect(within(row("fake.pdf")).getByText("not accepted")).toBeInTheDocument();
    expect(within(row("fake.pdf")).getByText(/only PDF, PNG and JPEG/)).toBeInTheDocument();
    await waitFor(() => expect(within(row("a.pdf")).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    await waitFor(() => expect(within(row("c.pdf")).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    expect(within(row("a.pdf")).getByText("matched PO-SS-001")).toBeInTheDocument();
    expect(row("a.pdf").querySelector("a")!.getAttribute("href")).toMatch(/^\/runs\/run1/);
  });

  it("stages no more files than allowed (the extras are not added), without uploading any", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await choose("1.pdf", "2.pdf", "3.pdf", "4.pdf");
    expect(screen.getByRole("alert")).toHaveTextContent("At most 3 files per upload: 1 not added.");
    expect(screen.queryByText("4.pdf")).toBeNull();
    expect(processBtn()).toHaveTextContent("Process 3 invoices");
    await choose("5.pdf");
    expect(screen.getByRole("alert")).toHaveTextContent("1 not added");
    expect(posts).toEqual([]);
  });

  it("a single file still goes straight to its live run view", async () => {
    mockFetch();
    const push = vi.spyOn(window.history, "pushState");
    render(<UploadScreen health={health} />);
    await choose("one.pdf");
    expect(push).not.toHaveBeenCalled();
    await process();
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
    await choose("x.pdf");
    await process();
    // with a PO context even one file stays on this page, so the outcome can be compared with the PO
    const list = await screen.findByRole("heading", { name: "This upload" });
    await waitFor(() => expect(within(list.parentElement!).getByText("Review")).toBeInTheDocument(), { timeout: 4000 });
    expect(posts).toEqual(["x.pdf"]);                                   // no PO hint is sent: the upload is the same request
  });
});

describe("staged upload", () => {
  const big = (name: string) => { const f = new File(["x"], name, { type: "application/pdf" }); Object.defineProperty(f, "size", { value: 21 * 1_048_576 }); return f; };
  it("choosing files makes no request; each row shows name, size and type; Process is disabled at 0", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    expect(processBtn()).toBeDisabled();
    expect(processBtn()).toHaveTextContent("Process 0 invoices");
    await choose("a.pdf", "b.pdf");
    expect(posts).toEqual([]);
    const staged = screen.getByRole("heading", { name: "Ready to process" }).parentElement!;
    const row = within(staged).getByText("a.pdf").closest("li")!;
    expect(row).toHaveTextContent("1 KB · application/pdf");
    expect(processBtn()).toHaveTextContent("Process 2 invoices");
    expect(processBtn()).not.toBeDisabled();
  });
  it("Remove takes a file out, Clear empties the list", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await choose("a.pdf", "b.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Remove a.pdf" }));
    expect(screen.queryByText("a.pdf")).toBeNull();
    expect(processBtn()).toHaveTextContent("Process 1 invoice");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByRole("heading", { name: "Ready to process" })).toBeNull();
    expect(processBtn()).toBeDisabled();
    expect(posts).toEqual([]);
  });
  it("marks too-large and wrong-type files and never sends them; Process sends exactly the acceptable ones, in order", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: [...files("b.pdf", "notes.txt"), big("huge.pdf")] } }); });
    const staged = screen.getByRole("heading", { name: "Ready to process" }).parentElement!;
    expect(within(staged).getByText("notes.txt").closest("li")!).toHaveTextContent("not a PDF or image");
    expect(within(staged).getByText("huge.pdf").closest("li")!).toHaveTextContent("too large");
    expect(processBtn()).toHaveTextContent("Process 1 invoice");
    await choose("a.pdf");                                              // marked rows count towards the cap of 3 ...
    expect(screen.getByRole("alert")).toHaveTextContent("1 not added");
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));   // ... and Remove frees a place
    await choose("a.pdf");
    expect(processBtn()).toHaveTextContent("Process 2 invoices");
    await process();
    await waitFor(() => expect(posts).toEqual(["b.pdf", "a.pdf"]));
    expect(screen.queryByRole("heading", { name: "Ready to process" })).toBeNull();
  });
  it("a PNG or JPEG is accepted by extension even without a MIME type", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    await act(async () => { fireEvent.change(screen.getByTestId("file-input"), { target: { files: [new File(["x"], "scan.JPG"), new File(["x"], "p.png")] } }); });
    expect(processBtn()).toHaveTextContent("Process 2 invoices");
  });
  it("dropping files stages them too, without a request", async () => {
    mockFetch();
    render(<UploadScreen health={health} />);
    const zone = screen.getByText("Drop invoices here").closest(".dropzone")!;
    await act(async () => { fireEvent.drop(zone, { dataTransfer: { files: files("d.pdf") } }); });
    expect(screen.getByText("d.pdf")).toBeInTheDocument();
    expect(posts).toEqual([]);
  });
});
