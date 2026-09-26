// The client-side copy of the server's per-line fit check (backend/app/pipeline/allocation.py `fit`), so the approve form can
// say before submitting whether a choice fits. The server re-checks everything; this only avoids a round trip.
// Money is handled in integer cents, never floats of currency amounts.
import type { LineChoice, LineOption, NeedsInput } from "./types";

export interface Tol { pct: number; abs: string; mode: "lesser_of" | "greater_of" }

export function toCents(s: string | null | undefined): number | null {
  if (s === null || s === undefined || s === "") return null;
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(s.trim());
  if (!m) return null;
  const cents = Number(m[2]) * 100 + Number((m[3] ?? "").padEnd(2, "0"));
  return m[1] ? -cents : cents;
}

export const fromCents = (c: number) => `${c < 0 ? "-" : ""}${Math.floor(Math.abs(c) / 100)}.${String(Math.abs(c) % 100).padStart(2, "0")}`;

// The whole-PO tolerance calculation, per line: the percentage part is floored to whole cents, and nothing remaining leaves none.
export function allowanceCents(remainingCents: number, tol: Tol): number {
  const pctPart = Math.floor((Math.max(remainingCents, 0) * tol.pct) / 100 + 1e-9);
  const absPart = toCents(tol.abs) ?? 0;
  return tol.mode === "lesser_of" ? Math.min(pctPart, absPart) : Math.max(pctPart, absPart);
}

export interface LineCheck { ok: boolean; message: string | null }

export function optionsOf(n: NeedsInput): LineOption[] {
  return [...n.candidates, ...n.other_lines];
}

// Check every reviewer choice in line order; lines choosing the same PO line use up its remaining amount in turn (as the server does).
export function checkChoices(needs: NeedsInput[], choices: Record<number, LineChoice | undefined>, tol: Tol): Record<number, LineCheck> {
  const used: Record<number, number> = {};
  const out: Record<number, LineCheck> = {};
  for (const n of [...needs].sort((a, b) => a.invoice_line_no - b.invoice_line_no)) {
    const choice = choices[n.invoice_line_id];
    if (!choice) { out[n.invoice_line_id] = { ok: false, message: "Choose a PO line or \"no specific line\"." }; continue; }
    if (choice.target === "unassigned") { out[n.invoice_line_id] = { ok: true, message: null }; continue; }
    const opt = optionsOf(n).find((o) => o.po_line_id === choice.po_line_id);
    const amount = toCents(n.amount) ?? 0;
    const remaining = opt ? toCents(opt.remaining_amount) : null;
    if (!opt || remaining === null) {
      out[n.invoice_line_id] = { ok: false, message: "That PO line has no amount to check against; choose another line or no specific line." };
      continue;
    }
    const b = remaining - (used[choice.po_line_id!] ?? 0);
    const allowance = allowanceCents(b, tol);
    if (amount - b <= allowance) {
      used[choice.po_line_id!] = (used[choice.po_line_id!] ?? 0) + amount;
      out[n.invoice_line_id] = { ok: true, message: null };
    } else {
      out[n.invoice_line_id] = { ok: false, message: `${fromCents(amount)} does not fit PO line ${opt.po_line_no}: ${fromCents(b)} remaining, allowance ${fromCents(allowance)}.` };
    }
  }
  return out;
}

export function initialChoices(needs: NeedsInput[]): Record<number, LineChoice> {
  const out: Record<number, LineChoice> = {};
  for (const n of needs) {
    out[n.invoice_line_id] = n.suggested.target === "po_line"
      ? { invoice_line_id: n.invoice_line_id, target: "po_line", po_line_id: n.suggested.po_line_id }
      : { invoice_line_id: n.invoice_line_id, target: "unassigned" };
  }
  return out;
}
