// Display helpers. Money stays a decimal string end to end: grouping is added to the text, never through a float.
import type { Decision, Outcome, StageName } from "./types";

export function money(value: unknown, currency?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  const s = String(value);
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(s);
  if (!m) return s;
  const [, sign, int, frac] = m;
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const text = `${sign}${grouped}${frac !== undefined ? "." + frac.padEnd(2, "0") : ".00"}`;
  return currency ? `${text} ${currency}` : text;
}

export const pct = (x: number | null | undefined, digits = 0) =>
  x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;

export const score = (x: number | null | undefined) => (x === null || x === undefined ? "—" : x.toFixed(2));

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

export function usd(value: unknown): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n === 0 ? "$0" : `$${n < 0.01 ? n.toFixed(4) : n.toFixed(n < 1 ? 4 : 2)}`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const STAGE_LABEL: Record<StageName, string> = {
  ingest: "Ingest", extract: "Extract", match: "Match", validate: "Validate", decide: "Decide", explain: "Explain", act: "Act",
};

export const STAGE_HINT: Record<StageName, string> = {
  ingest: "Check the file, store a copy, render the pages",
  extract: "Read the fields, with evidence and confidence",
  match: "Find the vendor and the purchase order",
  validate: "Run every rule against the facts",
  decide: "Take the most severe outcome",
  explain: "Write the reasoning from the audit trail",
  act: "Record the invoice and carry out the decision",
};

export const DECISION: Record<Decision, { label: string; tone: string; icon: string; meaning: string }> = {
  approve: { label: "Approve", tone: "approve", icon: "✓", meaning: "Passed every check. Ready for payment." },
  review: { label: "Review", tone: "review", icon: "!", meaning: "A person needs to look at this before it is paid." },
  request_info: { label: "Request info", tone: "info", icon: "?", meaning: "Critical data is missing or unclear. Ask the vendor." },
  reject: { label: "Reject", tone: "reject", icon: "✕", meaning: "Clearly invalid. It will not be paid." },
};

export const OUTCOME: Record<Outcome, { label: string; tone: string }> = {
  pass: { label: "Pass", tone: "pass" },
  flag: { label: "Flag", tone: "flag" },
  fail: { label: "Fail", tone: "fail" },
  info: { label: "Info", tone: "muted" },
};

export const FIELD_LABEL: Record<string, string> = {
  vendor_name: "Vendor", vendor_tax_id: "Vendor tax ID", vendor_address: "Vendor address", document_type: "Document type",
  invoice_number: "Invoice number", invoice_date: "Invoice date", currency: "Currency", po_reference: "PO reference",
  subtotal: "Subtotal", tax: "Tax", total: "Total",
};
export const HEADER_FIELDS = Object.keys(FIELD_LABEL);
export const MONEY_FIELDS = new Set(["subtotal", "tax", "total"]);

export const GROUNDING: Record<string, { label: string; tone: string; help: string }> = {
  exact: { label: "Exact", tone: "pass", help: "The source text was found on the page exactly." },
  normalized: { label: "Normalised", tone: "pass", help: "Found on the page after normalising spacing and case." },
  value_present: { label: "Value on page", tone: "pass", help: "The value is on the page; the snippet as quoted is not (typical of text layers)." },
  fuzzy: { label: "Fuzzy", tone: "flag", help: "Only a close match of the snippet was found; confidence capped at 0.75." },
  unavailable: { label: "No text layer", tone: "muted", help: "The page has no usable text layer to check against (a scan)." },
  no_source: { label: "No source", tone: "flag", help: "The model gave no source text; confidence capped at 0.50." },
  not_found: { label: "Not found", tone: "fail", help: "Neither the snippet nor the value is on the page; confidence capped at 0.40." },
  value_mismatch: { label: "Mismatch", tone: "fail", help: "The value disagrees with its own source text; confidence capped at 0.30." },
};

export function humanize(key: string): string {
  const s = key.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// One line per stage card, from the whitelisted `stage_completed` summary.
export function stageLine(stage: StageName, s: Record<string, unknown>): string {
  const n = (k: string) => (s[k] as number | undefined) ?? 0;
  switch (stage) {
    case "ingest": {
      if (s.failure_code) return `Not readable: ${humanize(String(s.failure_code))}`;
      const kind = String(s.media_type ?? "").replace("application/", "").replace("image/", "").toUpperCase();
      const pages = `${n("pages_processed")} page${n("pages_processed") === 1 ? "" : "s"}`;
      const text = s.text_layer === "usable" ? "text layer usable" : "no usable text layer";
      return [kind, pages, text].filter(Boolean).join(" · ");
    }
    case "extract": {
      if (s.degraded) return `Degraded: ${humanize(String(s.failure_code ?? "unknown"))}`;
      const bits = [`${n("fields_found")} of ${n("fields_total")} fields`, `${n("line_items")} line${n("line_items") === 1 ? "" : "s"}`];
      if (s.path) bits.push(humanize(String(s.path)));
      if (s.cost_usd !== undefined) bits.push(usd(s.cost_usd));
      return bits.join(" · ");
    }
    case "match": {
      const vendor = s.vendor ? String(s.vendor) : "vendor not resolved";
      if (s.matched_po) return `${vendor} → ${s.matched_po} (${score(s.top_score as number)})`;
      return `${vendor} · ${humanize(String(s.match_status ?? "no match"))}`;
    }
    case "validate": {
      const c = (s.counts as Record<string, number>) ?? {};
      return `${c.pass ?? 0} pass · ${c.flag ?? 0} flag · ${c.fail ?? 0} fail`;
    }
    case "decide":
      return s.decision ? DECISION[s.decision as Decision]?.label ?? String(s.decision) : "";
    case "explain":
      return s.source === "llm" ? `Written by ${s.model ?? "the model"}` : "Template from the audit trail";
    case "act": {
      const rows = (s.rows_written as Record<string, number>) ?? {};
      const parts = Object.entries(rows).map(([t, k]) => `${humanize(t)} ${k}`);
      return (s.downgraded_to_review ? "Approval withheld · " : "") + (parts.join(" · ") || "nothing written");
    }
  }
}
