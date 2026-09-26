import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { UploadScreen } from "../screens/Upload";
import { POListScreen } from "../screens/POList";

let urls: string[] = [];
function mockFetch() {
  urls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(url);
    const reply = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/api/runs")) return reply({ runs: [] });
    if (url.startsWith("/api/pos")) return reply({ pos: [] });
    return reply({});
  }));
}
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("the Invoices list decision filter (?decision=)", () => {
  it("asks for that decision and says so, with a way back to all runs", async () => {
    mockFetch();
    window.history.replaceState(null, "", "/invoices?decision=request_info");
    render(<UploadScreen health={null} />);
    expect(await screen.findByRole("heading", { name: "Runs the system decided: Request info" })).toBeInTheDocument();
    await waitFor(() => expect(urls.some((u) => u === "/api/runs?limit=50&decision=request_info")).toBe(true));
    expect(screen.getByRole("link", { name: "Show all runs" })).toHaveAttribute("href", "/invoices");
  });
  it("ignores an unknown decision", async () => {
    mockFetch();
    window.history.replaceState(null, "", "/invoices?decision=maybe");
    render(<UploadScreen health={null} />);
    expect(await screen.findByRole("heading", { name: "Recent runs" })).toBeInTheDocument();
    await waitFor(() => expect(urls).toContain("/api/runs?limit=12"));
  });
});

describe("the PO list currency filter (?currency=)", () => {
  it("asks for that currency, shows it, and can clear it", async () => {
    mockFetch();
    window.history.replaceState(null, "", "/pos?currency=inr");
    render(<POListScreen />);
    await waitFor(() => expect(urls).toContain("/api/pos?currency=INR"));
    expect(screen.getByText("INR")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "show all currencies" }));
    await waitFor(() => expect(urls[urls.length - 1]).toBe("/api/pos?"));
    expect(window.location.pathname + window.location.search).toBe("/pos");
  });
  it("also reads ?status=", async () => {
    mockFetch();
    window.history.replaceState(null, "", "/pos?status=open&currency=USD");
    render(<POListScreen />);
    await waitFor(() => expect(urls).toContain("/api/pos?status=open&currency=USD"));
    expect((screen.getByLabelText("Status") as HTMLSelectElement).value).toBe("open");
  });
});
