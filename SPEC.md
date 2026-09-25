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

*Assumptions added during M1 (rules engine). "Owner" = decided by the project owner in the M1 plan review; "(mine)" = my call, please check:*

21. **Facts snapshot (owner).** Evaluators are pure `(RunContext, params) -> RuleResult`, so the data they reason over is a read-only `RunFacts` snapshot on `RunContext`: vendors, POs (with ledger-derived consumption and lines), prior invoices, runtime settings. `engine/loader.py` is the ONLY engine module that touches SQLite. The snapshot is taken once per run; its models are frozen and each evaluator receives its own deep copy of the context, so nothing an evaluator does can leak. The snapshot loads whole tables (fine at demo scale; an indexed pre-filter is future work) (mine).
22. **Contract changes in 6.2 (owner-approved):** `RunContext` gains `facts`, `matched_vendor`, `match_status` (`matched | no_candidates | low_score | ambiguous`); `POCandidate` gains `breakdown`; `RuleResult` gains `outcome_key` and enforces outcome/severity coupling.
23. **Outcomes (owner).** `flag` = suspicious or ambiguous, `fail` = hard violation. Both are *triggered* and carry severity 1..3. `pass` and `info` are never triggered and always carry severity 0. A check whose essential input is null returns `info` ("not evaluable"), never `pass`. **Any null value is missing regardless of its confidence** (blank text counts as missing; zero does not).
24. **Fail-safe (owner).** A rule whose evaluator raises, returns something invalid, has invalid params, or has an unknown `type` produces a `flag` at the rule's default severity - never a silent pass. Per-outcome severities live in `params.severity_by_outcome` (values 1..3); `severity_on_trigger` is the default for any outcome key without an override. `severity_on_trigger` is a reserved key the engine injects into evaluator params.
25. **Engine floor (owner, extended).** The engine adds an `engine_floor` result that is not a rule and cannot be disabled. Final severity is at least 1 (config `engine_floor_severity`) UNLESS all of these hold: a PO was confidently and unambiguously matched (`match_status == matched`); the amount check was actually evaluable (matched PO in the snapshot, non-null amount, valid whole-cent amount, positive amount, currency present and equal to the PO's); every configured required field is non-null; every present required field meets the confidence threshold. It is computed from the context, not from rule results, so it holds even with `r_po_found`, the tolerance rule or the completeness rule disabled. It always appears in the trail with its reasons.
26. **Locked rules (owner).** `config.LOCKED_RULE_IDS = {r_duplicate_exact, r_vendor_status}`: the engine ignores `enabled=false` for them and records an info event saying so. Their params stay editable. Rules from source `nl` (or any LLM path) may only be ADDED - they must never disable or edit a builtin rule; only a human via settings can. A user/nl rule reusing a builtin id runs alongside it and cannot silence it.
27. **Tolerance (owner).** B = remaining PO balance before this invoice (total minus ledger sum), I = invoice amount, E = I - B. Allowance A = `min(floor(max(B,0) * pct/100), abs)` in mode `lesser_of` (default, stricter) or `max(...)` in `greater_of`, computed in integer minor units, rounded DOWN to whole cents. "Either / larger-of" is `greater_of`; "both" is `lesser_of`. E <= 0 passes; 0 < E <= A passes and the excess and allowance are recorded; E > A flags for review. With B <= 0 the percentage part is 0, so tolerance never compounds after an over-balance approval.
28. **Ledger and PO status on approval (owner).** An approval commits the invoice's FULL amount (never capped); a PO approved slightly over balance ends with a negative derived balance, shown as over-billed. Reversals are the exact negative. PO status is derived from the ledger: `open` (nothing committed), `partially_billed`, `fully_billed` (net committed >= total, including over-billed). `closed` is set only by a human and never changed automatically. Because §5 also stores a status, `r_po_status` treats a PO as fully billed if EITHER the stored status or the derived balance says so (the stricter reading). Writing ledger rows and updating PO status happens in M3; M1 defines and tests the pure derivation.
29. **Amount compared (owner).** The invoice `total` is compared to the PO balance (config `amount_compare_field`, default `total`; `subtotal` is available). **PO totals (including in seed data) are treated as tax-inclusive; a per-PO tax flag is future scope.**
30. **Duplicates (owner).** Only prior invoices whose effective status is in `counted_statuses` (default `approved, in_review, pending`) can be duplicated. A same-file-hash match against a `rejected` or `awaiting_info` prior raises a severity-1 flag ("resubmission"); an invoice-number-only match against those statuses is ignored, so a corrected resend after `request_info` is not rejected. Exact: same file hash (3), or same vendor + normalised invoice number with the same total (3) or a different / unknown total (1, possibly revised). Fuzzy (1): same vendor, same amount (`amount_tolerance`, default exact), dates within `days` (default 7), different number, same currency if both known, not the same file. Numbers are normalised (casefold, punctuation removed, leading zeros in digit runs removed). Null never equals null. The current run is always excluded. A second invoice against the same PO from the same vendor is NORMAL and not a duplicate by itself.
31. **Vendor resolution and PO matching (mine; all values in `config.MatchConfig`).** Vendor names are normalised (case, accents, punctuation, generic legal suffixes from a config list) and matched exactly against name/aliases, else by similarity >= 0.85; near-ties between different vendors are reported as ambiguous rather than guessed (an exact match is not ambiguous with a near miss). PO candidates are scored `0.40 reference + 0.25 vendor + 0.20 amount + 0.15 lines` (each signal 0..1, reported per candidate); an inferred reference is worth half, a fuzzy reference at most 0.6, a contained one ('1001' in 'PO-1001') 0.9. A PO is a candidate only if reference, vendor or line overlap supplies evidence. Confident = top score >= 0.5; ambiguous = top-two gap < 0.10 while the runner-up scores >= 0.4; `matched_po` is set only for a confident, unambiguous match. These are starting values, tuned on hand-written data only. Consequence worth knowing: with no PO reference, amount fit alone cannot separate two otherwise identical same-vendor POs (0.20 vs 0.11 is under the margin) - the engine escalates to review rather than guess.
32. **Arithmetic (mine).** Rounding allowance is `rounding_per_term` (default 0.01) times the number of terms in the comparison (one per line, N for a sum of N lines, 2 for subtotal + tax). If `tax.included_in_total` is true, total is compared to subtotal; if it is null with a tax present it is treated as excluded and the assumption is recorded in the result. Checks with null inputs are listed as skipped, not failed.
33. **Extraction-confidence rule (mine).** It covers header fields only (line-level confidence is out of scope), skips null fields (completeness owns those), and takes the threshold from the `settings` table via the facts snapshot.
34. **Determinism and trail (owner).** Rules run in a canonical order (builtin, then user, then nl, then id) regardless of input order. Audit events carry no timestamps or seq (assigned on persistence in M3). `detail` dicts are JSON-safe: Decimals as exact strings, dates ISO. The M1 `decide` stage only picks the decision from the results; explanation and actions arrive in M3.
35. **The twelve builtin rules and their severities (mine, provisional).** `r_vendor_status` 1 {new 1, blocked 3, unknown 1, ambiguous 1}; `r_po_found` 1 {no_reference 2, reference_not_found 1, no_confident_match 1}; `r_po_ambiguity` 1; `r_vendor_po_mismatch` 1; `r_currency_mismatch` 1; `r_tolerance_pct` 1 {over_tolerance 1, non_positive_total 1, invalid_amount 1}; `r_arithmetic` 1; `r_duplicate_exact` 3 {same_file_hash 3, same_vendor_number_same_total 3, same_vendor_number_different_total 1, resubmission 1}; `r_duplicate_fuzzy` 1; `r_required_fields` 2; `r_extraction_confidence` 1; `r_po_status` 1 {fully_billed 1, closed 3}. `r_vendor_po_mismatch` and `r_currency_mismatch` are new in M1 (the M0 seed had ten).
36. **Reference floor (owner).** A PO reached through a PO reference that is not an exact normalised match (`reference:contained` or `reference:fuzzy`) can never result in `approve`. The engine adds a second floor result, `engine_floor_reference` (severity `engine_floor_severity`, i.e. at least review), with the reason "reference X resembles PO Y". The target is the matched PO, or the top candidate if nothing matched. Like `engine_floor` it is not a rule and cannot be disabled. Exact normalised references (any formatting variant, explicit or inferred) are unaffected. **Extended (owner, 2026-09-25):** it also applies to any explicitly stated reference (`explicit` is not false) that does not match the PO at all (`reference:none`, the PO reached on vendor/amount/lines alone) or matches no PO whatsoever; an extractor-inferred reference (`explicit=false`) is exempt from those two cases. A validate run now yields 12 rule results plus 2 floor results.
37. **Ambiguity with a blocked vendor (owner).** `VendorMatch.candidate_vendor_ids` lists the vendors tied in an ambiguous match. `r_vendor_status` keeps severity 1 for `ambiguous`, sets `detail.blocked_candidate` (true/false) and `detail.candidate_vendors`, and names each blocked vendor in the message. `r_po_found` now describes truthfully how the PO was found (explicit / inferred / resembling reference / other signals).

*Assumptions added during M2 (ingest + extraction), stages 1-3. "Owner" = decided by the project owner in the M2 plan review; "(mine)" = my call, please check:*

38. **Contract additions (owner).** `ExtractedInvoice` gains `vendor_tax_id`, `vendor_address`, `document_type` (invoice / credit_note / proforma / quote / statement / receipt / other), `adjustments[]`, `line_items[].item_code` and `document_quality.contains_reader_instructions`. Evidenced fields also carry system-set `model_confidence` (the model's raw value) and `grounding`; `confidence` is the EFFECTIVE confidence (the model's, later capped by grounding), so every existing rule and floor keeps working. The LLM-facing model stays `extra="ignore"`. A separately hand-written wire schema (money as decimal strings) is what the API sees (see item 44); a drift test keeps the two in step.
39. **Adjustments and tax (owner).** The model returns an adjustment amount as printed (a positive magnitude for shipping, discount, credit and fee; signed for rounding and other); the system applies the sign by kind (discount/credit subtract, shipping/fee add, rounding/other keep the printed sign) and keeps `printed_amount`. `tax` is the TOTAL tax: if only component taxes are printed (CGST + SGST, state + county) the model sums them and says so in `extraction_notes`. `total` is the final amount payable (after discounts, incl. tax and shipping); a differing Balance Due is described in the notes. An Order ID / Order No is not a PO reference: `po_reference` is set only for a value printed with a purchase-order label (`explicit=true`).
40. **Currency, amounts, dates (owner).** Config `currency_symbol_map`: `$`->USD, EUR sign->EUR, GBP sign->GBP, rupee sign / `Rs` / `Rs.`->INR; other symbols (e.g. the yen sign) stay unmapped (value null + note, so `currency` counts as missing). The amount parser handles `1,234.56`, `1.234,56` and Indian `1,00,000.00`; in a document `1,234` / `1.234` are ambiguous, so grounding will accept both readings, while a model-returned `1,234` is read as a thousands group (the model is told to use a dot decimal). An ambiguous day/month date (03/04/2026) is returned as a best reading with confidence capped at 0.5 by the system, so it goes to review. Tax IDs are compared after removing case, spaces, hyphens and dots; the stored value stays as printed.
41. **Failure kinds and degradation (owner).** `vendor_side` (ask the vendor): password-protected or blank documents. `system_side` (a human reviews): corrupt/unrenderable files (our renderer may be the cause), missing API key, API/timeout/auth errors, refusal, schema failure after one repair retry, cost ceiling, unknown price. A failed extraction never raises: it degrades to an all-null extraction with a reason, and unreadable documents cost $0 (no model call). Rejected files (empty, oversize, wrong type by magic bytes) raise before any run folder is created. NOTE: routing system_side failures to `review` (rather than `request_info` via the completeness rule) needs the engine changes planned for Stage 4.
42. **LLM call policy (owner + mine).** Model name, prices and ceilings are config. Sonnet 5 runs adaptive thinking when `thinking` is omitted, so we send `thinking=disabled` with `effort=low`; if the API rejects that pair the client retries once with thinking omitted (effort kept), remembers it, and reports it (`llm-probe` and the CLI show which applies). Structured output via `output_config.format`. Transport retries/backoff are the SDK's (`llm_max_retries`, `llm_timeout_s`); we add exactly one schema-repair retry. Ceilings: $0.25 per run and $5.00 per session (process), enforced BEFORE a call from a pessimistic worst-case projection; cost is computed exactly (Decimal) from real usage. The key is read only from `ANTHROPIC_API_KEY`, blank counts as unset, and is scrubbed from all messages, logs and recorded fixtures.
43. **Ingest and path (owner + mine).** File type is decided by magic bytes; max 20 MB; max 10 pages (extra pages are not processed and the stage is flagged); pages are rendered as PNG (JPEG fallback under the API's 5 MB image limit) with the longest side <= 1568 px; SHA-256 of the original feeds duplicate detection. Path `auto`: text + images when the text layer is usable (>= 40 chars/page and >= 60% word-like characters), images only otherwise; the path used is recorded in `ExtractionMeta.path`.

44. **Wire schema is union-free (owner, after a live 400).** The API rejected the first wire schema: "too many parameters with union types (49 parameters with type arrays or anyOf) ... limit: 16". The wire schema (only what the API sees) now has ZERO unions, ZERO nulls and ZERO optional properties: each evidenced field is `{found, value, page, source_text, confidence}` with placeholders when absent (value "", page 0, source_text "", confidence 0); nullable booleans are the enum yes / no / unknown; `document_type` and `document_quality.type` use "unknown"; line items, adjustments and `extraction_notes` use "" and 0. `from_wire` converts back: found=false becomes null / page None / confidence 0 / source None (placeholders are ignored), page 0 becomes None, yes/no/unknown become True/False/None, and found=true with an empty value is treated as not found with a system note. A structurally wrong reply is a schema failure (one repair retry). The internal contract (SPEC 6.1, nullable) and all M1 code are unchanged. Documented API limits: additionalProperties:false on every object, no recursion, no numeric/string constraints, array minItems only 0 or 1; the documentation states NO numeric limit for optional parameters, properties, depth, enum size or schema size, so `test_wire_limits.py` enforces the one observed limit (16 unions, budget 0) plus conservative self-imposed budgets for the rest (currently 89 properties / 15 objects / 5 KB). `python -m app.llm.probe --schema` sends the real schema with a text-only prompt and is the authoritative check.

45. **Wire schema v3 and the structured-output switch (owner, after a second live 400).** The per-field-object wire schema of item 44 was ALSO rejected ("The compiled grammar is too large"). The header fields are now ONE array of entries `{name, found, value, page, source_text, confidence, flag}` (`flag` = yes/no/unknown; it carries `po_reference.explicit` and `tax.included_in_total`, and is "unknown" for every other name), beside small flat arrays for `line_items` and `adjustments` and a small `document_quality` object: 5 objects / 29 properties / 1.9 KB (was 15 / 89 / 5.3 KB), still zero unions, nulls and optionals. **Live result: the API ACCEPTED this schema on the first probe** (`llm-probe --schema --all`: json_schema accepted, prompt_json works, `thinking=disabled` + `effort=low` accepted with no fallback; each probe cost about $0.0087). The converter maps missing names to not found, keeps the first of duplicate names (with a note), and ignores and logs unknown names; for replies that were not grammar-constrained it is lenient in the safe direction (numbers accepted as strings, a missing confidence is 0, a missing kind is "other"). Config `llm_structured_output` = `json_schema` (default: send the strict schema) | `prompt_json` (send NO schema; the schema is included in the prompt as text, the reply is parsed tolerantly - code fences and text around the JSON are accepted, a truncated reply is reported as invalid JSON - validated by the converter and Pydantic, with the same single repair retry). A schema/grammar rejection is `LLMSchemaError` (`schema_rejected`); the extraction CLI stops with exit code 4 and a message naming `LLM_STRUCTURED_OUTPUT=prompt_json` instead of showing a degraded run (the eval script will reuse this in Stage 5); the pipeline itself still degrades to review. Prompt `extract-v3`: only the OUTPUT FORMAT block and the two flag mentions changed; the extraction rules and field definitions are unchanged.

46. **Currency read from a symbol has a configured confidence (owner).** A currency that arrives as a bare symbol (`$`, `€`, `£`, `₹`, `Rs`) is mapped to an ISO code by config (`currency_symbol_map`) - a mapping, not a reading - so its EFFECTIVE confidence is `currency_symbol_confidence` (default 0.85, above the 0.8 review threshold) whatever the model said. The model's raw score stays in `model_confidence` and a `[system]` note records both ("currency confidence set to 0.85 from configuration (symbol-derived; the model reported 0.7)"). A model that reported 0 (or omitted the score) is respected: no boost. A three-letter code the document printed is a reading and keeps the model's confidence. Reason: the live check returned 0.70 for `$` on a clean invoice (24429), which would have sent it to review. **Risk (accepted):** a `$` invoice from a vendor that actually bills in another dollar currency (AUD, CAD...) is read as USD with confidence 0.85, and is only caught if the matched PO's currency differs from the mapped one (`r_currency_mismatch`). With no matched PO nothing catches it. Lower `currency_symbol_confidence` below 0.8 to send every symbol-only currency to review.
47. **Grounding (owner-amended; SPEC 6.1 evidence).** After post-processing, every non-null field, line item and adjustment is checked against the page text. Statuses and caps (all config, `Settings.grounding`): `no_source` 0.50; `value_mismatch` 0.30 (the value disagrees with its own source_text: amounts compared as `Decimal` across US/EU/Indian grouping, dates across both day/month readings, identifiers after stripping punctuation and case); `exact` / `normalized` no cap; `value_present` 0.85 (the snippet is not on the page but the VALUE is, which is what label-separated text layers produce: "Subtotal: $5,141.76" is not a substring of the real PDFs' text although 5,141.76 is on the page); `fuzzy` 0.75 (snippet within 0.90 similarity, at least 8 characters, and the value NOT found on the page; a fuzzy snippet whose value is on the page is `value_present`); `not_found` 0.40; `unavailable` (no usable text for that page: no cap, but no-source and mismatch still apply). Effective confidence = min(current, cap): grounding never raises a confidence. Adjustments are checked against the printed magnitude. A snippet found on another page corrects the page number (noted). Counts go to `ExtractionMeta.grounding`, one summary line and one line per problem go to `extraction_notes`, and a `grounding` audit event is recorded. A bug in the check degrades the run to review (`grounding_error`) instead of passing unchecked values.
48. **Reader instructions (owner).** A deterministic scan (`injection_patterns`, config) over every page's text layer, or the model's own `contains_reader_instructions=yes`, sets `ExtractionMeta.injection_suspected` (with evidence) and records a `reader_instructions_detected` event. The document stays data: nothing it says changes a rule. The engine floor adds three reasons that cannot be disabled: `extraction_degraded` (any failure kind), `pages_truncated`, and `reader_instructions_detected`; each forces at least review. The floor result is still ONE `engine_floor` result carrying all reasons (15 results per run with 13 rules, unchanged structure).
49. **System-side failures are a review, vendor-side ones a request (owner).** When the extraction failed on our side (renderer, config, API, schema, cost ceiling, grounding error) the fields are unknown, not missing: `r_required_fields` and the no-reference outcome of `r_po_found` are `not_evaluable` (info) so nothing asks the vendor to resend, and the `extraction_degraded` floor gives `review`. Vendor-side failures (password-protected, blank) keep `r_required_fields` at severity 2, so the decision is `request_info`. `r_vendor_status` already stands down without a vendor name.
50. **Tax-id vendor resolution (owner).** `resolve_vendor` tries the extracted `vendor_tax_id` first (`method="tax_id"`, score 1.0). Ids compare ignoring case, spaces, hyphens and dots; a letters-only label or country prefix on ONE side is ignored when at least 5 characters remain ("EIN 12-3456789" = "12-3456789", "GB123456789" = "123456789"), two different prefixes never match ("GB..." vs "DE..."). A tax id that resolves to one vendor while the name resolves to a different vendor (or several) is reported as `ambiguous` with every involved vendor as a candidate (a blocked one is spelled out by `r_vendor_status`, severity stays 1). The same id on two vendor records is ambiguous. An id no vendor has falls back to the name. `r_vendor_status` runs when a vendor was resolved by tax id even if the invoice printed no name.
51. **`r_document_type` (13th builtin rule, owner).** `invoice` passes; any other type (credit note, proforma, quote, statement, receipt, other) flags severity 1 (`not_an_invoice`); a missing type is not evaluable. Param `allowed` (default `["invoice"]`). Nothing is auto-approved as a credit note.
52. **Arithmetic includes adjustments; a missing tax line means "no tax printed" (mine, owner-approved for adjustments).** Expected total = subtotal + sum(adjustment amounts, already signed by kind) + tax (or without tax when it is included in the total). Allowance = `rounding_per_term` x (2 + number of adjustments). An adjustment with a null amount skips the check rather than guessing. When NO tax line was found the invoice is checked as tax = 0 (check `subtotal_plus_adjustments_equals_total`, `tax_assumed_zero=true`): a total above subtotal + adjustments is an unexplained difference and flags, instead of the old silent skip. This changes the M1 behaviour that skipped the check for a null tax.
53. **Unit-price rounding allowance on line math (mine; owner approved).** A printed unit price is rounded to the cent, so quantity x unit price can differ from the printed amount by up to half a cent per unit. The line-math check therefore allows `rounding_per_term + |quantity| x unit_price_rounding` (default 0.005, config `arithmetic_unit_price_rounding`, rule param `unit_price_rounding`; 0 restores the strict behaviour). Found on real invoice 24429: 4 x 461.48 = 1,845.92 against a printed 1,845.94. Only the line-math check widens; sums, subtotals and totals keep the per-term allowance.

54. **The eval script never makes a live call without `--live` (owner).** `python -m app.extraction.eval` calls the paid API only when `--live` is given; without `--live` and without `--replay DIR` it prints a refusal, makes no call, reads no key, creates no run folder and exits 5. `--record DIR` also needs `--live`; `--replay` cannot be combined with `--live` or `--record` (usage error, exit 2) and its folder must exist. The refusal happens before any client exists: the real client is constructed in exactly one function (`build_client`), behind an `allow_live` check, and a structural test enforces both. With `--live` and no key: exit 3, nothing sent. A live run announces the number of calls, the estimate (about $0.023 per one-page invoice, measured) and the ceiling (`--max-cost`, default the session ceiling in config; the per-run ceiling still applies); a cost-ceiling stop ends the run early and lists what was not processed. A schema rejection ends the run with the same message as the extraction CLI (exit 4). An empty or missing folder prints "0 invoices found in ..." and exits 0 without needing any flag. Exit code 1 means at least one file could not be extracted.
55. **The answer key is `data/manifest.md` and only human-verified entries are scored (owner).** Per file: a `##` heading with the exact file name, optional bullet notes, and one fenced block labelled `expected` holding JSON (fields as in SPEC 6.1, plus `line_items` `{description?, amount, quantity?, unit_price?}` and `adjustments` `{kind, amount}` with discounts and credits negative). An omitted field is not scored; an explicit `null` means "must be absent". Every entry carries `"verified": true|false`; anything but a JSON `true` (including a missing key) is a draft and is never scored, and the report says how many entries are unverified and how many files are unlabelled. Bad JSON, unknown fields, duplicate headings, unclosed fences and non-boolean `verified` are reported as warnings and the entry is skipped; a missing manifest is empty.
56. **How the eval scores (mine).** Verdict per expected field: correct, missed (value expected, null extracted), hallucinated (null expected, value extracted; for lists an extracted item that matches nothing), wrong (a different value). Money compares as exact `Decimal`, dates as ISO, currency upper-cased, tax ids ignoring case/space/hyphen/dot, other strings ignoring case, punctuation and spacing. Line items match one-to-one by exact amount plus, when the key gives one, a description contained in (or containing) the extracted one, plus quantity and unit price when given; adjustments match by kind and signed amount; an extra extracted list item counts against that list's accuracy. Files whose extraction failed (or was rejected at ingest) are listed under failures and are not scored, so an API outage does not read as bad accuracy. The report shows per-field accuracy, the mean confidence of correct and wrong answers (effective, after grounding, and the model's raw score), grounding status counts, path counts, tokens, cost and API time.
57. **`--draft-manifest` never verifies anything (owner).** For files with NO manifest heading it appends a draft (`"verified": false`, every extracted value, explicit nulls for what was not found, a note saying to check every value against the document); an existing entry, verified or not, is never edited (the run says which files were skipped), failed files get no draft, and the writer refuses to write `verified: true`. A human flips the flag after checking. Drafts are not scored, on this run or the next.

58. **PO reference cases in `r_po_found` (owner).** Three cases, all with the shipped severities. (a) A PO reference that is STATED but matches no PO (`reference_not_found`) is severity 2, so the decision is `request_info` (this was severity 1 before; the owner described it as request_info, so it is now 2 in the builtin params; the reference floor still applies on top). (b) NO reference at all (null) but a confident, unambiguous PO match on vendor, amount and lines (`matched_without_reference`) is severity 1, so the decision is `review`: a person confirms a strong suggestion instead of starting from nothing (this was a pass before). (c) No reference AND no confident match (`no_reference`, including a low-score best candidate) stays severity 2, `request_info`. A resembling but weak reference (`no_confident_match`) stays severity 1; an ambiguous match is left to `r_po_ambiguity`. Consequence: an invoice that prints no PO number, such as the two SuperStore samples, can no longer be auto-approved even with a perfect match.

59. **Tax printed only as a RATE is null, never computed (owner; prompt extract-v4).** Found on a real scanned Indian GST invoice: CGST 9.00 and SGST 9.00 are printed as percentages with no amount, and the live model (extract-v3) returned `tax` = 882.00 that it had calculated itself. The prompt now says: `tax` is the tax AMOUNT printed on the document (several printed component amounts are summed, as before); if only rates or percentages are printed, `tax` is not found, the model must never compute an amount, and the printed rates go in `extraction_notes`. The tax `flag` (`included_in_total`) carries the model's best read even when `tax` is not found (a rate-only invoice whose total equals the line total is most likely tax-inclusive); the wire converter keeps that one flag when found=false (every other flag is still ignored). With a null tax and `included_in_total = true` the arithmetic rule compares subtotal + adjustments to the total. A prompt change cannot be proven offline: the recorded live reply predates it (its 882.00 is still capped at 0.3 by grounding and flagged by `r_arithmetic`, so an ignored prompt is still safe). Changing the prompt changes every recording key, so recordings made with extract-v3 no longer replay.
60. **Currency written in words is grounded (owner).** The grounding check consults a small config table `currency_name_map` (rupee(s), indian rupee(s), dollar(s), US/Australian/Canadian dollar(s), euro(s), pound(s), pound(s) sterling, yen, Swiss franc(s) -> ISO code) before calling a currency `value_mismatch`, in both the value-agrees-with-its-snippet check and the value-on-page fallback. Whole words, case-insensitive, and only the LONGEST matching name counts ("Australian Dollars" vouches for AUD, not for the "dollars" = USD entry). It is consulted for currency only. A bare "rupees" is INR, so a Pakistani or Sri Lankan invoice read as PKR/LKR from that word alone is a mismatch (conservative); a name not in the table is still a mismatch. The default table is replaced, not merged, when configured.

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