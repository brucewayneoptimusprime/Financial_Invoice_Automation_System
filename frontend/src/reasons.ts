// Plain-language reasons for people; the internal identifiers go behind "Technical details".
//
// The template explanation writes each reason in one of two fixed forms (backend/app/pipeline/digest.py; pinned by
// backend/tests/pipeline/test_reason_format.py):
//   "<rule_id> (<rule name>) - <outcome_key>, severity <n>: <message>"
//   "Engine floor (<reason_code>): <message>"
// and the review-queue reason is "Review: " + parts joined by " | ", each "<rule_id> (<outcome_key>): <message>" or a floor text.
// A reason written by the model is already prose. Anything else is shown as it is, never hidden.
import { FIELD_LABEL } from "./format";

export interface Technical {
  ruleId?: string;
  ruleName?: string;
  outcome?: string;
  severity?: number;
  floorCode?: string;
  facts: string[];
}

export interface PlainReason {
  text: string;
  technical: Technical | null;
}

const RULE_FORM = /^(\S+) \((.*?)\) - ([A-Za-z0-9_]+), severity (\d+): ([\s\S]+)$/;
const FLOOR_FORM = /^Engine floor \(([A-Za-z0-9_]+)\): ([\s\S]+)$/;
const REVIEW_PART = /^(\S+) \(([A-Za-z0-9_]+)\): ([\s\S]+)$/;

const FIELD_WORDS: [RegExp, string][] = Object.entries(FIELD_LABEL)
  .sort((a, b) => b[0].length - a[0].length)
  .map(([id, label]) => [new RegExp(`\\b${id}\\b`, "g"),
                         /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label]);   // keep "PO ..."

// "vendor_name" -> "vendor name" (the same labels as the fields table).
export function humanizeFields(text: string): string {
  let out = text;
  for (const [re, label] of FIELD_WORDS) out = out.replace(re, label);
  return out;
}

function sentence(text: string): string {
  const t = humanizeFields(text.trim());
  if (!t) return t;
  const cap = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?…]$/.test(cap) ? cap : cap + ".";
}

export function plainReason(raw: string, facts: string[] = []): PlainReason {
  const text = raw.trim();
  let m = RULE_FORM.exec(text);
  if (m) {
    return { text: sentence(m[5]), technical: { ruleId: m[1], ruleName: m[2], outcome: m[3], severity: Number(m[4]), facts } };
  }
  m = FLOOR_FORM.exec(text);
  if (m) {
    return { text: sentence(m[2]), technical: { ruleId: "engine_floor", ruleName: "Engine floor", floorCode: m[1], facts } };
  }
  return { text: sentence(text), technical: facts.length ? { facts } : null };
}

export function reviewReasons(raw: string): PlainReason[] {
  const body = raw.replace(/^Review:\s*/, "");
  return body.split(" | ").filter((p) => p.trim()).map((part) => {
    const floor = FLOOR_FORM.exec(part.trim());
    if (floor) return { text: sentence(floor[2]), technical: { ruleId: "engine_floor", ruleName: "Engine floor", floorCode: floor[1], facts: [] } };
    const m = REVIEW_PART.exec(part.trim());
    if (m) return { text: sentence(m[3]), technical: { ruleId: m[1], outcome: m[2], facts: [] } };
    return { text: sentence(part), technical: null };
  });
}
