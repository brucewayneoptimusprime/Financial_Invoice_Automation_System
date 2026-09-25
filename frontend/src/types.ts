// Shapes returned by the local API (backend/app/api). Money arrives as exact decimal strings.

export type Decision = "approve" | "review" | "request_info" | "reject";
export type Outcome = "pass" | "flag" | "fail" | "info";
export type StageName = "ingest" | "extract" | "match" | "validate" | "decide" | "explain" | "act";
export type StageStatus = "waiting" | "running" | "ok" | "flagged" | "failed";
export type Mode = "live" | "replay" | "offline";

export const STAGES: StageName[] = ["ingest", "extract", "match", "validate", "decide", "explain", "act"];

export interface AuditEvent {
  seq: number;
  stage: string;
  event_type: string;
  rule_id: string | null;
  outcome: Outcome;
  message: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface Health {
  status: string;
  mode: Mode;
  model: string;
  session_spent_usd: string | null;
  session_ceiling_usd: string;
  run_ceiling_usd: string;
  queue_length: number;
  max_file_bytes: number;
  max_files_per_upload?: number;
}

export interface RunRow {
  id: string;
  source_file: string | null;
  status: string;                 // running | completed | failed | queued | rejected
  started_at?: string | null;
  finished_at?: string | null;
  final_decision?: Decision | null;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number | null;
  model?: string | null;
}

export interface EvidencedField {
  value: string | number | null;
  page: number | null;
  source_text: string | null;
  confidence: number;
  model_confidence: number | null;
  grounding: string | null;
  explicit?: boolean | null;
  included_in_total?: boolean | null;
}

export interface LineItem {
  description: string | null;
  item_code?: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string | null;
  page: number | null;
  source_text: string | null;
  confidence: number;
  model_confidence: number | null;
  grounding: string | null;
}

export interface Adjustment {
  kind: string | null;
  description: string | null;
  amount: string | null;
  printed_amount: string | null;
  page: number | null;
  source_text: string | null;
  confidence: number;
  grounding: string | null;
}

export interface Extracted {
  [field: string]: unknown;
  line_items: LineItem[];
  adjustments: Adjustment[];
  document_quality: { type: string | null; issues: string[]; contains_reader_instructions: boolean | null };
  extraction_notes: string | null;
}

export interface RuleRow {
  rule_id: string;
  name: string;
  kind: "rule" | "floor";
  outcome: Outcome;
  severity: number;
  outcome_key: string | null;
  message: string;
  detail: Record<string, unknown>;
}

export interface Candidate {
  po_number: string;
  score: number | null;
  breakdown: Record<string, number>;
  reasons: string[];
}

export interface Explanation {
  text: string;
  one_line: string;
  reasons: { text: string; facts: string[] }[];
  next_step: string;
  source: "template" | "llm" | string;
  model: string | null;
  fallback_reason: string | null;
  cost_usd?: string;
}

export interface Draft {
  id: number;
  kind: "vendor_email" | "notification" | string;
  to: string | null;
  subject: string;
  body: string;
  status: string;
}

export interface StageView {
  stage: StageName;
  status: StageStatus;
  duration_ms: number | null;
  summary: Record<string, unknown>;
}

export interface RunView {
  run: RunRow;
  stages: StageView[];
  decision: Decision | null;
  explanation: Explanation | null;
  invoice: Record<string, unknown> | null;
  lines: { line_no: number; description: string | null; quantity: string | null; unit_price: string | null; amount: string | null }[];
  extracted: Extracted | null;
  vendor: (Record<string, unknown> & { record?: { name: string; status: string; tax_id: string | null; country: string | null } }) | null;
  match: { status: string | null; matched_po: string | null; message: string | null; candidates: Candidate[] };
  rules: RuleRow[];
  aggregate: { decision: Decision; final_severity: number; triggered: { rule_id: string; severity: number }[] } | null;
  actions: {
    ledger: Record<string, unknown> | null;
    ready_for_payment: boolean;
    escalated: Record<string, unknown> | null;
    review: { id: number; reason: string; status: string; resolution: string | null }[];
    drafts: Draft[];
  };
  pages: number[];
  stage_costs: Record<string, string>;
  error: { message: string; error_type: string } | null;
  event_count: number;
}

// A run that is still waiting in the queue, or could not start.
export interface PendingView {
  run: { id: string; status: "queued" | "running" | "rejected" };
  rejection?: { code: string; message: string } | null;
}

// ---------------------------------------------------------------------------------------------- purchase orders

export interface Vendor { id: number; name: string; status: "approved" | "new" | "blocked"; tax_id: string | null; country: string | null }

export interface POListRow {
  id: number; po_number: string; vendor_id: number; vendor: string; vendor_status: string; currency: string;
  total: string; balance: string; issued_date: string | null; status: string; invoice_count: number; source: string | null;
}

export interface PODetail {
  po: { id: number; po_number: string; vendor_id: number; vendor: string; vendor_status: string; vendor_tax_id: string | null;
        currency: string; issued_date: string | null; status: string };
  amounts: { total: string; committed: string; balance: string; awaiting_review: string; over_billed: boolean };
  lines: { line_no: number; description: string | null; quantity: string | null; unit_price: string | null; amount: string | null }[];
  invoices: { invoice_id: number; run_id: string | null; historic: boolean; invoice_number: string | null; invoice_date: string | null;
              currency: string | null; total: string | null; decision: Decision | null; status: string; source_file: string | null;
              run_status: string | null; cost_usd: number | null; started_at: string | null }[];
  ledger: { id: number; type: string; amount: string; invoice_id: number; created_at: string }[];
  considered_in: { run_id: string; source_file: string; started_at: string; score: number | null; decision: Decision | null;
                   run_status: string; match_status: string | null; matched_po: string | null }[];
  provenance: Record<string, unknown>;
}

export interface POLineInput { description: string; quantity: string; unit_price: string; amount: string }
export interface NewVendorInput { name: string; tax_id: string; country: string }
export interface POInput {
  po_number: string; vendor_id: number | null; currency: string; total: string; issued_date: string; lines: POLineInput[];
}
export interface POIssue { field: string; level: "error" | "warning"; code: string; message: string }
export interface ValidateResult { issues: POIssue[]; can_save: boolean; lines_sum: string | null }

export interface PODraftView {
  draft_id: string;
  status: "ok" | "failed";
  source: "text" | "document";
  failure: { code: string; message: string } | null;
  values: { po_number: string | null; currency: string | null; total: string | null; issued_date: string | null;
            vendor_name: string | null; vendor_tax_id: string | null;
            lines: { description: string | null; quantity: string | null; unit_price: string | null; amount: string | null }[] };
  marks: Record<string, { found: boolean; confidence: number; source_text: string | null; page: number | null; grounding: string | null }>;
  suggested_vendor_id: number | null;
  new_vendor: { name: string; tax_id: string; country: string } | null;
  vendor_hint: string | null;
  warnings: string[];
  notes: string[];
  issues: POIssue[];
  lines_sum: string | null;
  pages: number[];
  model: string | null;
  cost_usd: string;
}
