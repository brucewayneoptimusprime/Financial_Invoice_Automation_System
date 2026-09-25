// Thin, typed wrappers over the local API. All paths are relative: Vite proxies /api to the backend.
import type { AuditEvent, Health, PendingView, RunRow, RunView } from "./types";

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
