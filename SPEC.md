# Invoice Processing Agent (PS-1): Project Spec

> Purpose: fix the *plumbing* (data model, contracts, decision set, guardrails) so every Claude Code session builds against the same shape. Logic (thresholds, matching heuristics, edge cases) is intentionally left open. Rename this file to `CLAUDE.md` if you want Claude Code to load it automatically.

---

## 1. What we are building

A process that takes **one vendor invoice (PDF or image)** as input and produces a **clear, reasoned decision** as output, with every intermediate step visible.

The invoice is the only per-run input. The procurement side (POs, vendors, invoice history, rules) is **preloaded state** in the database, mirroring the real AP workflow where someone looks up the PO in a spreadsheet.

**Pipeline:** `ingest -> extract -> match -> validate -> decide -> act`

Each stage writes to an audit log. A live run view and a dashboard read from that log.

## 2. Design principles (non-negotiable)

1. **The AI reads, the rules decide, the system acts.** LLMs do extraction, explanation writing, and drafting. Ordinary code applies rules and picks the decision.
2. **Stricter, never looser.** Rules from users or LLMs may only *raise* the severity of an outcome. Nothing but a human reviewer can lower it. Enforced in the engine, not in a prompt.
3. **Escalate when unsure.** Low confidence or missing critical fields route to a human. Never guess.
4. **Every claim has evidence.** Extracted fields carry page and source text. Rule outcomes carry the numbers that drove them.
5. **Rules are data, not code.** Thresholds, vendor lists and policies live in the database and can change without a deploy.
6. **General mechanisms, not case-specific hacks.** Do not write logic for a particular test invoice. Edge cases are used to *test* mechanisms, not to define them.
7. **Anything outward-facing is a draft.** Vendor emails and notifications are shown in the UI. Nothing is sent automatically.
8. **State is durable.** SQLite, not in-memory. PO balances are derived from the ledger, never stored as a mutable counter.
9. **Demo safety.** One-command reset to a known seed state. No dependency on anything that can fail live except the LLM API (and handle that failure gracefully).

## 3. Stack (default; change only with reason)

- **Backend:** Python, FastAPI, SQLite (SQLAlchemy or plain sqlite3), Pydantic for all contracts
- **LLM:** Claude API (vision + text). Model name in config, never hard-coded
- **Frontend:** React (Vite) with a designed, intuitive UI. Fallback: Streamlit if time-critical
- **Live progress:** Server-Sent Events from the backend to the run view
- **PDF handling:** render pages to images for the vision model. Also try embedded text first and compare, to record which path was used

## 4. Decision set and severity

| Decision | Severity | Meaning |
|---|---|---|
| `approve` | 0 | Passes all checks. Ready for payment |
| `review` | 1 | Ambiguous or off. Goes to a human queue |
| `request_info` | 2 | Critical data missing or unreadable. Ask the vendor |
| `reject` | 3 | Clearly invalid (e.g. duplicate, blocked vendor) |

**Final decision = highest severity across all triggered rules.** Rules only add flags. An unresolved low-confidence critical field forces at least `review`.

*(Open question for later: whether `request_info` should outrank `reject`. Keep the ordering in one config constant.)*

## 5. Data model (SQLite)

```
vendors
  id, name, aliases(json), tax_id, country, status[approved|new|blocked], created_at

purchase_orders
  id, po_number(unique), vendor_id, currency, total_amount, issued_date,
  status[open|partially_billed|fully_billed|closed], meta(json)

po_lines
  id, po_id, line_no, description, quantity, unit_price, amount

invoices
  id, run_id, vendor_id(nullable), invoice_number(nullable), invoice_date(nullable),
  currency, subtotal, tax, total, po_id(nullable),
  decision, status, source_file, file_hash, extracted(json), created_at

invoice_lines
  id, invoice_id, line_no, description, quantity, unit_price, amount

ledger_entries              -- source of truth for PO consumption
  id, po_id, invoice_id, amount, type[commit|reversal], created_at
  -- PO balance = po.total_amount - SUM(ledger_entries.amount). Never stored directly.

runs
  id, source_file, status[running|completed|failed], started_at, finished_at,
  final_decision, tokens_in, tokens_out, cost_usd, model

audit_events                -- feeds live run view, trace, and dashboard
  id, run_id, seq, stage, event_type, rule_id(nullable),
  outcome[pass|flag|fail|info], message, detail(json), created_at

rules
  id, name, type, params(json), severity_on_trigger, source[builtin|user|nl],
  enabled, original_text(nullable), created_at

review_queue
  id, run_id, reason, status[open|resolved], resolution(nullable), resolved_at

drafts
  id, run_id, kind[vendor_email|notification], to, subject, body,
  status[draft|marked_sent], created_at

settings
  key, value(json)          -- e.g. tolerance defaults, confidence threshold
```

Notes:
- Historic approved invoices are seeded as ledger entries so demos start "mid-story".
- Keep enough on `invoices` to support duplicate detection: `file_hash`, `invoice_number`, `vendor_id`, `total`, `invoice_date`.

## 6. Contracts

### 6.1 Extracted invoice (output of the extract stage)

Every field is wrapped so evidence and confidence travel with the value.

```json
{
  "vendor_name":    {"value": "Acme Supplies Ltd", "page": 1, "source_text": "ACME SUPPLIES LTD", "confidence": 0.97},
  "invoice_number": {"value": null, "page": null, "source_text": null, "confidence": 0.0},
  "invoice_date":   {"value": "2026-03-14", "page": 1, "source_text": "14 Mar 2026", "confidence": 0.93},
  "currency":       {"value": "USD", "...": "..."},
  "po_reference":   {"value": "PO-1001", "explicit": true, "...": "..."},
  "subtotal":       {"value": 2000.00, "...": "..."},
  "tax":            {"value": 160.00, "included_in_total": false, "...": "..."},
  "total":          {"value": 2160.00, "...": "..."},
  "line_items": [
    {"description": "...", "quantity": 40, "unit_price": 50.0, "amount": 2000.0, "page": 1, "confidence": 0.9}
  ],
  "document_quality": {"type": "scanned|native", "issues": ["skewed", "low_resolution"]},
  "extraction_notes": "free text from the model about ambiguities"
}
```

Rules for extraction:
- Return `null` for anything not found. Never invent values.
- `po_reference.explicit=false` means the PO was inferred and must be matched by other signals.
- Validate against a Pydantic schema. On schema failure, retry once, then mark the run `review` with the reason.

### 6.2 Stage interface

```python
class RunContext:      # accumulates state through the pipeline
    run_id, source_file, extracted, matched_po, candidates, rule_results, decision, ...

class StageResult:
    stage: str
    status: Literal["ok", "flagged", "failed"]
    outputs: dict
    events: list[AuditEvent]   # emitted live over SSE and persisted

def run_stage(ctx: RunContext) -> StageResult: ...
```

Stages are independent and individually testable. Each stage may be run with hand-written input, without the LLM.

### 6.3 Rule schema

```json
{
  "id": "r_tolerance_pct",
  "name": "Invoice within tolerance of PO balance",
  "type": "amount_tolerance",
  "params": {"pct": 2.0, "abs": 50.0},
  "severity_on_trigger": 1,
  "source": "builtin",
  "enabled": true,
  "original_text": null
}
```

- The engine looks up an evaluator by `type` and passes `params` plus the run context. It does not care where the rule came from.
- **Guardrail:** engine computes `final = max(severity of triggered rules)`. There is no code path where a `user` or `nl` rule lowers severity.
- Rules of type `nl` store `original_text` so the audit trail shows the policy in the client's own words.
- If a natural-language policy cannot be expressed with the available schema and data, the translator must say so and refuse to create a rule.

**Built-in rule families (parameters open, tune later):** vendor status, PO found, PO ambiguity, amount vs PO balance with tolerance, arithmetic consistency (lines, tax, totals), duplicate detection (exact and fuzzy), required-field completeness, extraction confidence, PO status (fully billed).

## 7. LLM roles

| Role | Where | Constraint |
|---|---|---|
| Extractor | extract stage | Structured output, evidence and confidence, `null` for missing |
| Match assistant | match stage | May *rank candidate POs* with reasoning. Final match must still pass deterministic checks |
| Reviewer | after rules | May only **add** flags (severity >= 1). Cannot override a rule |
| Explainer | decide stage | Writes the plain-language reasoning **from the audit trail**, not from memory |
| Drafter | act stage | Drafts vendor email for `request_info` / `reject` |
| Policy translator (optional, see milestones) | settings | NL policy to rule JSON, shown for user confirmation before saving |

Log tokens and cost per run. Show cost per invoice on the dashboard.

## 8. Actions per decision

| Decision | System action |
|---|---|
| `approve` | Write ledger commit, update PO status, mark ready for payment |
| `review` | Create review-queue item with reason and evidence. Reviewer can approve/reject in UI, which writes to the ledger accordingly |
| `request_info` | Draft vendor email listing exactly which fields are missing or unclear |
| `reject` | Mark rejected, draft vendor email with the reason |

Every action is recorded in `audit_events`.

## 9. UI requirements

The UI is graded. Keep it clean, intentional and easy to demo.

1. **Upload / run screen:** drop an invoice, start a run.
2. **Live run view:** each stage appears as it executes with status, key outputs and timing. Expandable detail per stage.
3. **Result view:** the decision, plain-language reasoning, and the trace. Extracted fields show the source page and text. Each rule shows pass/flag/fail with the numbers.
4. **Dashboard:** run history, decision breakdown, status, PO balances, cost per run, filter and search.
5. **Review queue:** open items with approve/reject controls.
6. **Drafts:** vendor emails with a "mark as sent" button.
7. **Settings / rules:** view and toggle rules, edit thresholds. (NL policy input if built.)
8. **Reset demo data** button or command.

## 10. Test data policy

- **Use real-looking invoices**, sourced from public datasets and sample invoices, not model-generated PDFs. Mix native PDFs, scans, and photos or skewed images.
- **Build the PO dataset around the invoices** (vendors, amounts, line items), with some POs already partially consumed.
- **Controlled variants** of real invoices (changed number, re-scan, cropped total, split across two invoices) are allowed for scenarios that do not occur naturally.
- Keep a `data/` folder with `invoices/`, `seed.json`, and a `manifest.md` listing what each file is and what it is meant to exercise.
- **Edge cases are not pre-defined here.** They are chosen after the general pipeline works, then added to the manifest.

## 11. Assumptions (edit and keep; these are referenced in the pitch)

1. 2-way match (invoice vs PO). Goods-receipt (3-way) is a future extension.
2. The invoice is the only per-run input. POs, vendors and history are preloaded.
3. One invoice may reference at most one PO (multi-PO invoices are future scope).
4. Single currency per run. No FX conversion.
5. Credit notes are out of scope.
6. Vendor emails are drafted, not sent.
7. Tolerance and thresholds are configurable defaults, not universal truths.

*Assumptions added during M0 (decided with the project owner unless marked "(mine)"):*

8. **This file is the spec.** It was `readme.md` and is now `SPEC.md`; `CLAUDE.md` points to it.
9. **2-decimal currencies only.** Money is stored as INTEGER minor units (cents) so SQL `SUM()` is exact. Currencies with 0 or 3 decimals (JPY, KWD) are unsupported. All conversion goes through `app/money.py`; amounts with more than 2 decimals are rejected, not rounded. `quantity` and `unit_price` stay decimal TEXT (never summed in SQL). `runs.cost_usd` is REAL because it is telemetry with sub-cent precision, not ledger money.
10. **Ledger sign convention.** `commit` amounts are > 0 and `reversal` amounts are < 0 (DB CHECK), so PO balance = `total_amount - SUM(amount)`.
11. **Rules may only escalate.** `severity_on_trigger` must be 1..3 (0 is rejected by both Pydantic and a DB CHECK). Rules with `source = nl` must carry `original_text`.
12. **Decision vs effective outcome.** `runs.final_decision` is the system's original decision and is never modified. Human resolutions go in `review_queue.resolution` and update `invoices.status`, which is the effective outcome. `invoices.decision` is the decision at run time. `invoices.status` values (mine): `pending | approved | in_review | awaiting_info | rejected`. `review_queue.resolution` values (mine): `approved | rejected`.
13. **Tolerances live in rule params**, not in `settings`. Builtin rules are seeded from config defaults. `settings` holds only non-rule values (`confidence_threshold`, `model_override`). Builtin rule severities and the params other than tolerance and required fields are provisional (mine) and will be tuned in M1. Seeding uses INSERT OR IGNORE, so re-running init never overwrites edits.
14. **Pydantic strictness.** Internal models and the rule schema use `extra="forbid"`. The LLM-facing extracted-invoice model uses `extra="ignore"` and logs the ignored keys. Missing data is a valid state: every field's value may be null with confidence 0, and a field omitted entirely is treated the same (mine). Missing or low-confidence data is handled by rules, never by schema failure. Null value with non-zero confidence is not rejected (mine).
15. **Keys and nullability (mine).** `runs.id` and `rules.id` are TEXT; all other ids are INTEGER. `invoices.run_id` is nullable (seeded historic invoices have no run). `ledger_entries.invoice_id` is NOT NULL. `audit_events.rule_id` has no foreign key so audit history survives rule deletion. `drafts."to"` is quoted because `to` is an SQL keyword. `audit_events` is unique on `(run_id, seq)`.
16. **Timestamps** are UTC ISO-8601 TEXT. Seed records carry fixed `created_at` values so a reset reproduces identical state.
17. **Reset** drops all tables in place (works while another process holds the file open on Windows), then re-inits and re-seeds. It wipes user-created rules and changed settings, and refuses to touch a non-empty file that is not a SQLite database (mine).
18. **Migrations** are minimal: `PRAGMA user_version` tracks the schema version; any version other than the current one is an error until a real migration exists.
19. **Placeholder defaults (mine, open items in section 14):** confidence threshold 0.8, tolerance 2% / 50.00 absolute (from the section 6.3 example), and the default required fields `vendor_name, invoice_number, invoice_date, currency, total`. All are config values.
20. **Contract details left open by the spec (mine):** `RunContext` fields are `run_id, source_file, file_hash, started_at, extracted, matched_po, candidates, rule_results, decision`. I added `POCandidate` and `RuleResult` models. `AuditEvent.run_id` and `.seq` are optional until persisted. `stage` and `event_type` are free non-empty strings, not enums. `EvidencedField` line items carry an optional `source_text` beyond the 6.1 example.

## 12. Milestones (ordered by dependency, not by date)

Each milestone must be runnable and verified before the next begins.

- **M0 Foundations:** repo layout, config, DB schema and migrations, seed loader, reset command, Pydantic models for all contracts.
- **M1 Rules engine (no LLM, no UI):** rule schema, evaluators, severity aggregation with the escalate-only guardrail, ledger-derived PO balance. Tested with hand-written extracted-invoice JSON.
- **M2 Extraction:** PDF/image to the extracted-invoice contract with evidence and confidence. Tested against the real invoice set.
- **M3 Pipeline:** connect all stages, persist audit events, decision plus explanation, actions and drafts. Runs end-to-end from the command line.
- **M4 API and live run view:** FastAPI endpoints, SSE stage stream, upload and run screens.
- **M5 Dashboard, review queue, drafts, settings.**
- **M6 Hardening:** choose and add 2-4 edge cases, using the general mechanisms. Add failure handling (LLM timeout, unreadable file). Re-run the whole set.
- **M7 Optional:** natural-language policy input with a confirm step, and the escalate-only LLM reviewer.
- **M8 Ship:** deployment or local run instructions, demo script, rehearsal on the exact inputs, screen recording.

## 13. Working agreements for Claude Code

- Read this file first. Ask before changing anything in sections 4-6.
- Small, verifiable steps. Run tests after each change and show the results.
- Never hard-code behavior for a specific test file or vendor.
- Keep rules, thresholds and prompts in config or data, not scattered in code.
- Log every stage decision to `audit_events`.
- When something is ambiguous, pick a reasonable assumption, add it to section 11, and continue.
- Prefer simple and reliable over clever. A working end-to-end run beats a broader half-built one.

## 14. Open items (decide later, do not block the build)

- Exact tolerance values and duplicate-detection thresholds
- Confidence threshold for escalation
- The 2-4 named edge cases (choose after M3/M5)
- Hosted vs local demo (pending HR reply)
- Whether to build M7