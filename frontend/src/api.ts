// Thin, typed wrappers over the API. Every request goes through apiFetch / apiUrl (apiBase.ts): relative paths locally (the Vite
// proxy), the Render URL and the access token when deployed.
import { apiFetch, apiUrl } from "./apiBase";
import type { ApprovePreview, ApproveResult, AuditEvent, CrossCheckInfo, CrossCheckReport, Dashboard, ERPImport, ERPPreview, GlobalSettings, POSettings, SettingsEvent, SettingsPORow, GmailImportOutcome, GmailLabelsResult, GmailSearchCost, GmailSearchResult, GmailStatus, Health, LineChoice, NeedsInput, NewVendorInput, PendingView, PODetail, PODraftView,
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

export const getHealth = () => apiFetch("/api/health").then((r) => json<Health>(r));
export const listRuns = (limit = 12, decision = "") =>
  apiFetch(`/api/runs?limit=${limit}${decision ? `&decision=${encodeURIComponent(decision)}` : ""}`).then((r) => json<{ runs: RunRow[] }>(r));
export const getRun = (id: string) => apiFetch(`/api/runs/${encodeURIComponent(id)}`).then((r) => json<RunView | PendingView>(r));
export const pageUrl = (id: string, n: number) => apiUrl(`/api/runs/${encodeURIComponent(id)}/pages/${n}`, true);

export function isRunView(v: RunView | PendingView): v is RunView {
  return "stages" in v;
}

export async function uploadInvoice(file: File): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("file", file, file.name);
  return apiFetch("/api/runs", { method: "POST", body: form }).then((r) => json<{ run_id: string }>(r));
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
  const es = new EventSource(apiUrl(`/api/runs/${encodeURIComponent(id)}/events`, true));
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
export const listVendors = () => apiFetch("/api/vendors").then((r) => json<{ vendors: Vendor[] }>(r));
export const listPOs = (q = "", status = "", currency = "") =>
  apiFetch(`/api/pos?${new URLSearchParams({ ...(q ? { q } : {}), ...(status ? { status } : {}), ...(currency ? { currency } : {}) })}`)
    .then((r) => json<{ pos: POListRow[] }>(r));
export const getPO = (id: number | string) => apiFetch(`/api/pos/${encodeURIComponent(String(id))}`).then((r) => json<PODetail>(r));

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
  apiFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

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
  return apiFetch("/api/pos/drafts/document", { method: "POST", body: form }).then((r) => json<PODraftView>(r));
}
export const draftPageUrl = (draftId: string, n: number) => apiUrl(`/api/pos/drafts/${encodeURIComponent(draftId)}/pages/${n}`, true);

// ---------------------------------------------------------------------------------------------- review queue (one item at a time)
export const listReview = (status: "open" | "resolved" = "open") =>
  apiFetch(`/api/review-queue?status=${status}`).then((r) => json<{ items: ReviewListItem[]; open_count: number }>(r));
export const getReviewItem = (id: number | string) => apiFetch(`/api/review-queue/${encodeURIComponent(String(id))}`).then((r) => json<ReviewDetail>(r));

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

// ---------------------------------------------------------------------------------------------- simulated ERP feed (no model, no cost)
// The preview writes nothing; only erpImport() saves, and only the POs the person ticked.
export const erpPreview = () => apiFetch("/api/erp/preview").then(async (r) => {
  if (r.status === 422 || r.status === 404) {
    const b = (await r.json().catch(() => ({}))) as { error?: string; message?: string };
    throw new ApiError(r.status, b.error ?? "erp_feed", b.message ?? "The simulated ERP feed could not be read.");
  }
  return json<ERPPreview>(r);
});
export const erpImport = (feedSha256: string, poNumbers: string[]) =>
  act<ERPImport & { preview?: ERPPreview; po_numbers?: string[] }>("/api/erp/import",
                                                                 { feed_sha256: feedSha256, po_numbers: poNumbers, confirm: true });

export const getDashboard = (recent = 8, review = 5) => apiFetch(`/api/dashboard?recent=${recent}&review=${review}`).then((r) => json<Dashboard>(r));

// ---------------------------------------------------------------------------------------------- Gmail import (read-only)
// Search writes nothing; only gmailImport() queues files, and only the attachments the person ticked.
export const gmailStatus = () => apiFetch("/api/gmail/status").then((r) => json<GmailStatus>(r));
// A search from the manual box ({query}) or from a plain-English sentence ({sentence}); a refused translation answers 422 with
// the model's query (if any) and the problems, so the panel can put it in the manual box to edit.
export type GmailSearchBody = { query: string } | { sentence: string };
export const gmailSearch = (body: GmailSearchBody) =>
  act<GmailSearchResult & { problems?: string[]; query?: string | null; notes?: string; reason?: string; cost?: GmailSearchCost }>(
    "/api/gmail/search", body);
export const gmailImport = (searchId: string, items: { message_id: string; part_id: string }[]) =>
  act<{ items: GmailImportOutcome[]; queued: number; problems?: string[]; fits?: number }>(
    "/api/gmail/import", { search_id: searchId, items, confirm: true });
export const gmailLabels = (searchId: string) => act<GmailLabelsResult>("/api/gmail/labels", { search_id: searchId });
export const gmailConnectStart = () => post("/api/gmail/oauth/start", {}).then((r) => json<{ authorization_url: string }>(r));
export const gmailDisconnect = () => act<{ disconnected: boolean; revoked: boolean; message?: string }>("/api/gmail/disconnect", { confirm: true });

// ---------------------------------------------------------------------------------------------- rules settings (no model, no cost)
// Changes apply to invoices processed from now on; each changed value is logged. A 422 carries `problems: {key: reason}`.
export type SettingsProblems = Record<string, string>;
export const getSettings = () => apiFetch("/api/settings").then((r) => json<GlobalSettings>(r));
export const saveGlobalSettings = (body: { values?: Record<string, unknown>; rules?: Record<string, boolean>; restore?: string[] }) =>
  act<GlobalSettings & { problems?: SettingsProblems }>("/api/settings/global", body);
export const listSettingsPOs = (q = "") =>
  apiFetch(`/api/settings/pos${q ? `?q=${encodeURIComponent(q)}` : ""}`).then((r) => json<{ pos: SettingsPORow[] }>(r));
export const getPOSettings = (id: number) => apiFetch(`/api/settings/pos/${id}`).then((r) => json<POSettings>(r));
export const savePOSettings = (id: number, body: { values?: Record<string, unknown>; rules?: Record<string, boolean | null> }) =>
  act<POSettings & { problems?: SettingsProblems }>(`/api/settings/pos/${id}`, body);
export const settingsHistory = (params: { scope?: string; po_id?: number; limit?: number } = {}) =>
  apiFetch(`/api/settings/history?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}`)
    .then((r) => json<{ events: SettingsEvent[] }>(r));

// ---------------------------------------------------------------------------------------------- cross-check documents (report only)
// crossCheckInfo() calls no model and sends no file. Only crossCheckAnalyse() does, when the person clicks Analyze; it writes nothing.
export const crossCheckInfo = (poId: number) => apiFetch(`/api/pos/${poId}/crosscheck`).then((r) => json<CrossCheckInfo>(r));
export async function crossCheckAnalyse(poId: number, files: File[]): Promise<CrossCheckReport> {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  return apiFetch(`/api/pos/${poId}/crosscheck`, { method: "POST", body: form }).then((r) => json<CrossCheckReport>(r));
}
