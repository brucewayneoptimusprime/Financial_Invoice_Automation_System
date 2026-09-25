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
