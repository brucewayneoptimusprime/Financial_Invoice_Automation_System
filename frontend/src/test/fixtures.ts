// Event streams and run views recorded from the real backend (replay mode for the two real invoices; the "synthetic_*" ones are
// labelled controlled variants generated offline by the backend's test helpers). See STATUS.md, M4 stage 4.
import type { AuditEvent, RunView } from "../types";
import ss10963Sse from "./fixtures/ss_10963.sse.txt?raw";
import iqSse from "./fixtures/iq_scan.sse.txt?raw";
import approveSse from "./fixtures/synthetic_approve.sse.txt?raw";
import requestInfoSse from "./fixtures/synthetic_request_info.sse.txt?raw";
import failedSse from "./fixtures/synthetic_failed.sse.txt?raw";
import ss10963View from "./fixtures/ss_10963.view.json";
import iqView from "./fixtures/iq_scan.view.json";
import approveView from "./fixtures/synthetic_approve.view.json";
import requestInfoView from "./fixtures/synthetic_request_info.view.json";
import failedView from "./fixtures/synthetic_failed.view.json";

export interface Frame { event?: string; id?: string; data?: unknown; retry?: string }

export function parseSse(text: string): Frame[] {
  const out: Frame[] = [];
  for (const block of text.split("\n\n")) {
    if (!block.trim()) continue;
    const f: Frame = {};
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue;
      const i = line.indexOf(":");
      const key = line.slice(0, i);
      const value = line.slice(i + 1).replace(/^ /, "");
      if (key === "data") f.data = JSON.parse(value);
      else (f as Record<string, string>)[key] = value;
    }
    if (Object.keys(f).length) out.push(f);
  }
  return out;
}

export const auditOf = (frames: Frame[]) => frames.filter((f) => f.event === "audit").map((f) => f.data as AuditEvent);
export const endOf = (frames: Frame[]) => frames.find((f) => f.event === "end")?.data as { status: string; decision: string | null };

export const STREAMS = {
  ss10963: parseSse(ss10963Sse), iq: parseSse(iqSse), approve: parseSse(approveSse),
  requestInfo: parseSse(requestInfoSse), failed: parseSse(failedSse),
};
export const VIEWS = {
  ss10963: ss10963View as unknown as RunView, iq: iqView as unknown as RunView, approve: approveView as unknown as RunView,
  requestInfo: requestInfoView as unknown as RunView, failed: failedView as unknown as RunView,
};
