// Thin, typed wrappers over the local API. All paths are relative: Vite proxies /api to the backend.
import type { ApprovePreview, ApproveResult, AuditEvent, Health, LineChoice, NeedsInput, NewVendorInput, PendingView, PODetail, PODraftView,
              POInput, POIssue, POListRow, ReviewDetail, ReviewListItem, RunRow, RunView, ValidateResult, Vendor } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let code = "http_error";
    let message = `The server answered ${res.status}.`;
    try {
      const body = await res.json();
      code = body.error ?? code;
      message = body.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}

export const getHealth = () => fetch("/api/health").then((r) => json<Health>(r));
export const listRuns = (limit = 12) => fetch(`/api/runs?limit=${limit}`).then((r) => json<{ runs: RunRow[] }>(r));
export const getRun = (id: string) => fetch(`/api/runs/${encodeURIComponent(id)}`).then((r) => json<RunView | PendingView>(r));
export const pageUrl = (id: string, n: number) => `/api/runs/${encodeURIComponent(id)}/pages/${n}`;

export function isRunView(v: RunView | PendingView): v is RunView {
  return "stages" in v;
}

export async function uploadInvoice(file: File): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("file", file, file.name);
  return fetch("/api/runs", { method: "POST", body: form }).then((r) => json<{ run_id: string }>(r));
}

export interface StreamHandlers {
  onAudit(e: AuditEvent): void;
  onQueued(state: string): void;
  onEnd(end: { status: string; decision: string | null }): void;
  onRejected(r: { code: string; message: string }): void;
  onConnection(state: "connecting" | "open" | "lost"): void;
}

// The browser's EventSource reconnects on its own and sends Last-Event-ID, so a dropped connection resumes where it stopped.
export function streamRun(id: string, h: StreamHandlers): () => void {
  const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
  let done = false;
  h.onConnection("connecting");
  es.onopen = () => h.onConnection("open");
  es.addEventListener("audit", (m) => h.onAudit(JSON.parse((m as MessageEvent).data)));
  es.addEventListener("queued", (m) => h.onQueued(JSON.parse((m as MessageEvent).data).state));
  es.addEventListener("end", (m) => {
    done = true;
    es.close();
    h.onEnd(JSON.parse((m as MessageEvent).data));
  });
  es.addEventListener("rejected", (m) => {
    done = true;
    es.close();
    h.onRejected(JSON.parse((m as MessageEvent).data));
  });
  es.onerror = () => {
    if (!done) h.onConnection(es.readyState === EventSource.CLOSED ? "lost" : "connecting");
  };
  return () => {
    done = true;
    es.close();
  };
}

// ---------------------------------------------------------------------------------------------- purchase orders
export const listVendors = () => fetch("/api/vendors").then((r) => json<{ vendors: Vendor[] }>(r));
export const listPOs = (q = "", status = "") =>
  fetch(`/api/pos?${new URLSearchParams({ ...(q ? { q } : {}), ...(status ? { status } : {}) })}`).then((r) => json<{ pos: POListRow[] }>(r));
export const getPO = (id: number | string) => fetch(`/api/pos/${encodeURIComponent(String(id))}`).then((r) => json<PODetail>(r));

// The form's values -> what the API expects (empty strings become nulls; empty lines are kept so validation can name them).
export function poPayload(po: POInput, newVendor: NewVendorInput | null) {
  const nz = (s: string) => (s.trim() === "" ? null : s.trim());
  return {
    po: { po_number: nz(po.po_number), vendor_id: newVendor ? null : po.vendor_id, currency: nz(po.currency.toUpperCase()),
          total: nz(po.total), issued_date: nz(po.issued_date),
          lines: po.lines.map((l) => ({ description: nz(l.description), quantity: nz(l.quantity), unit_price: nz(l.unit_price), amount: nz(l.amount) })) },
    new_vendor: newVendor ? { name: newVendor.name.trim(), tax_id: nz(newVendor.tax_id), country: nz(newVendor.country) } : null,
  };
}

const post = (url: string, body: unknown) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const validatePO = (po: POInput, nv: NewVendorInput | null) =>
  post("/api/pos/validate", poPayload(po, nv)).then((r) => json<ValidateResult>(r));

export class SaveRefused extends Error {
  constructor(public status: number, message: string, public issues: POIssue[]) { super(message); }
}

// Save: the ONLY call that creates a purchase order. It sends what is on the form (plus the draft id, for provenance).
export async function savePO(po: POInput, nv: NewVendorInput | null, draftId: string | null) {
  const r = await post("/api/pos", { ...poPayload(po, nv), draft_id: draftId });
  if (r.status === 201) return (await r.json()) as { po_id: number; vendor_id: number; warnings: POIssue[] };
  let body: { message?: string; issues?: POIssue[]; detail?: unknown } = {};
  try { body = await r.json(); } catch { /* not JSON */ }
  throw new SaveRefused(r.status, body.message ?? `The server answered ${r.status}.`, body.issues ?? []);
}

// Model drafts: NOTHING is saved by these; the draft pre-fills the form, and only savePO() saves.
export const draftFromText = (text: string) => post("/api/pos/drafts/text", { text }).then((r) => json<PODraftView>(r));
export async function draftFromDocument(file: File): Promise<PODraftView> {
  const form = new FormData();
  form.append("file", file, file.name);
  return fetch("/api/pos/drafts/document", { method: "POST", body: form }).then((r) => json<PODraftView>(r));
}
export const draftPageUrl = (draftId: string, n: number) => `/api/pos/drafts/${encodeURIComponent(draftId)}/pages/${n}`;

// ---------------------------------------------------------------------------------------------- review queue (one item at a time)
export const listReview = (status: "open" | "resolved" = "open") =>
  fetch(`/api/review-queue?status=${status}`).then((r) => json<{ items: ReviewListItem[]; open_count: number }>(r));
export const getReviewItem = (id: number | string) => fetch(`/api/review-queue/${encodeURIComponent(String(id))}`).then((r) => json<ReviewDetail>(r));

export interface ActionOutcome<T> { ok: boolean; status: number; body: T & { error?: string; message?: string } }

async function act<T>(url: string, body: unknown): Promise<ActionOutcome<T>> {
  const r = await post(url, body);
  let parsed: any = {};
  try { parsed = await r.json(); } catch { /* not JSON */ }
  return { ok: r.ok, status: r.status, body: parsed };
}

export const approveItem = (id: number, stateToken: string, allocations: LineChoice[], note: string | null) =>
  act<ApproveResult & { needs_input?: NeedsInput[]; problems?: { invoice_line_id: number; message: string; code: string }[];
                        preview?: ApprovePreview; blocked_by?: { code: string; message: string }[] }>(
    `/api/review-queue/${id}/approve`, { confirm: true, state_token: stateToken, allocations, note: note || null });
export const rejectItem = (id: number, reason: string | null) =>
  act<{ status: string }>(`/api/review-queue/${id}/reject`, { confirm: true, reason: reason || null });
