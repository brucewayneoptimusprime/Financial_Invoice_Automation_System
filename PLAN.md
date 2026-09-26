# PLAN: M2 — Ingest and Extraction

Status: **approved (2026-09-25) with the changes listed in §16. Build order: stages 1–3, then STOP for the owner's live checks; stage 4 starts only on the owner's word.**
Project context: an invoice-processing agent. "The AI reads, the rules decide, the system acts" (SPEC.md §2). M0 (foundations) and M1 (rules engine, 602 tests) are done and approved. M2 turns one PDF/PNG/JPG into the `ExtractedInvoice` contract (SPEC §6.1) with per-field evidence and confidence, feeding the existing engine.

## 1. Scope

**In:** ingest stage; extraction stage (vision + embedded text); one LLM client module; prompts module; cost accounting and ceilings; grounding check; failure degradation; mocked test suite; live tests (opt-in); eval script with answer key.
**Out:** persistence of runs/audit events to SQLite (M3), API/SSE/UI (M4), explanation/drafting LLM roles (M3), edge-case selection (M6).
**Not changed by M2 unless approved below:** SPEC sections 4–6 semantics. Every touch of M1 code is listed in §5.5.

## 2. Verified facts and constraints

Checked against the current Claude API documentation (not memory):

| Topic | Fact | Consequence |
|---|---|---|
| Model | `claude-sonnet-5`, $2 / $10 per 1M input/output tokens (matches your figures) | Model name and prices live in config, never in code. |
| Thinking | On Sonnet 5, **omitting `thinking` runs adaptive thinking**. `{"type":"disabled"}` is accepted. Lowest effort is `low`. | We send `thinking={"type":"disabled"}` **and** `output_config.effort="low"` explicitly. The first live run confirms the API accepts the pair; if not, we keep `low` only (config flag). |
| Structured output | `output_config={"format":{"type":"json_schema","schema":{...}}}`; supported on Sonnet 5. Schema limits: `additionalProperties:false` on every object, no numeric/string constraints, no recursion. | A separate hand-written **wire schema** is sent; Pydantic re-validates (ranges, enums) afterwards. |
| Retries/timeouts | The SDK already retries 408/409/429/5xx and connection errors with exponential backoff (`max_retries`), and takes a `timeout`. | We configure the SDK (no home-grown retry loop) and add exactly one schema-repair retry of our own. |
| SDK | `anthropic` 1.8.0 (1.x, built on `httpx2`). Errors are typed (`RateLimitError`, `AuthenticationError`, `APITimeoutError`, …). | Error mapping is a most-specific-first chain. |
| API key | Read only from `ANTHROPIC_API_KEY` (env or your local `.env` via settings, as `SecretStr`). | I never read, print, log or commit `.env`. It is already gitignored and is not in any commit. |
| Environment | Windows; `pathlib` only; no shell-specific code. `data/invoices/` is empty; there is no `data/manifest.md` yet. | The eval script must work on an empty folder. |

**Key handling rules.** (1) One module builds the client; a missing/empty key raises `LLMConfigError("ANTHROPIC_API_KEY is not set …")`, never a crash or a bare 401. (2) The extraction stage catches it and degrades (see §10), so everything except live tests works with no key. (3) An autouse test fixture blanks `ANTHROPIC_API_KEY` for every non-live test, so a developer's real key in `.env` can never be picked up by a mocked test. (4) A canary-key test proves the key never appears in logs, exceptions, `repr`, run metadata or recorded fixtures.

## 3. Module layout

```
backend/app/
  llm/                     # the ONLY place that talks to Anthropic
    errors.py              # LLMError tree: LLMConfigError, LLMTimeout, LLMTransientError, LLMRefused,
                           #   LLMBadRequest, CostCeilingExceeded, PriceNotConfigured
    client.py              # LLMClient Protocol; AnthropicClient (SDK); LLMRequest / LLMResponse dataclasses
    pricing.py             # config price table -> Decimal cost (input, output, cache read/write)
    budget.py              # CostTracker: per-run + per-session ceilings, pre-call worst-case check, thread-safe
    replay.py              # RecordingClient / ReplayClient (fixture files; secrets never recorded)
  ingest/
    validate.py            # sniff type by magic bytes, size, empty, spoofed extension
    store.py               # sha256, copy into data/runs/<run_id>/, safe file names
    render.py              # PDF/image -> page images (pypdfium2 + Pillow), EXIF fix, downscale
    textlayer.py           # per-page embedded text + "usable?" assessment
    stage.py               # run_ingest_stage(ctx, path) -> StageResult
  extraction/
    prompts.py             # PROMPT_VERSION, system prompt, page-content builder (the ONLY prompt file)
    wire.py                # JSON schema sent to the API + wire -> ExtractedInvoice
    postprocess.py         # currency-symbol map, date/amount normalisation, source-page repair
    grounding.py           # the grounding check (§9)
    injection.py           # deterministic scan for text addressed to the AI
    extractor.py           # orchestration: build request, call, validate, one repair retry, degrade
    stage.py               # run_extract_stage(ctx, client=None) -> StageResult
    cli.py                 # python -m app.extraction.cli <file>
    eval.py, manifest.py   # python -m app.extraction.eval  (answer key parser + accuracy report)
  models/extraction_meta.py   # ExtractionMeta, IngestInfo (system-side, extra="forbid")
backend/tests/{llm,ingest,extraction}/ ; backend/tests/fixtures/llm/   # recorded/hand-written responses
data/runs/<run_id>/        # gitignored: original.<ext>, pages/page-N.png, meta.json, llm/ (no images, no key)
data/manifest.md           # answer key (format in §12.3)
```
New dependencies: `anthropic>=1.8`, `pypdfium2>=5`, `Pillow>=12` (runtime); `reportlab` (dev only, to generate test PDFs at test time — no binary fixtures are committed).

## 4. Configuration additions (all in `config.py`, all env-overridable)

| Group | Keys (default) |
|---|---|
| LLM | `model_name` (claude-sonnet-5); `llm_timeout_s` (60); `llm_max_retries` (2, SDK); `llm_max_output_tokens` (4096); `llm_thinking` (disabled); `llm_effort` (low); `llm_cache_system_prompt` (false); `schema_repair_retries` (1) |
| Prices | `llm_prices` = `{model: {input_per_mtok, output_per_mtok, cache_read_mult, cache_write_mult}}`; seeded with Sonnet 5 = 2.00 / 10.00 / 0.1 / 1.25. **Unknown model → `PriceNotConfigured` before any call** (a ceiling cannot be enforced without a price). |
| Ceilings | `cost_ceiling_per_run_usd` (0.25); `cost_ceiling_per_session_usd` (5.00) — "session" = one process (API server or eval run) |
| Ingest | `allowed_media_types` (pdf, png, jpeg); `max_file_bytes` (20 MB); `max_pages` (10); `runs_dir` (data/runs); `render_max_side_px` (1568); `render_dpi` (150) |
| Extraction | `extraction_mode` (auto \| vision \| text_and_vision \| text); `text_min_chars_per_page` (40); `text_min_wordlike_ratio` (0.6); `text_max_chars_per_page` (15000); `grounding_caps` (below); `currency_symbol_map` ({"$":"USD","€":"EUR","£":"GBP","₹":"INR","Rs":"INR","Rs.":"INR"}); `injection_patterns` |

## 5. Contract changes (exact fields — for your approval)

### 5.1 `ExtractedInvoice` additions (LLM-facing, stays `extra="ignore"`, unknown keys logged)
| Field | Type | Why |
|---|---|---|
| `vendor_tax_id` | evidenced string | Strongest vendor identifier; matching currently relies on name only. |
| `vendor_address` | evidenced string (one block) | Disambiguates look-alike vendors; shown to reviewers. |
| `document_type` | evidenced enum: `invoice, credit_note, proforma, quote, statement, receipt, other` | Credit notes are out of scope (SPEC §11.4); a statement or quote must never be approved as an invoice. |
| `adjustments[]` | `{kind: shipping\|discount\|credit\|fee\|rounding\|other, description, printed_amount, amount, page, source_text, confidence}` | Shipping/discounts outside line items otherwise make the arithmetic rule flag correct invoices. **The model returns the amount as printed (absolute); the system applies the sign by kind**: discount/credit subtract, shipping/fee add, rounding/other keep the printed sign. `printed_amount` keeps the model's value, `amount` is the signed value. |
| `line_items[].item_code` | optional string | Stronger line matching than free-text descriptions. |
| `document_quality.contains_reader_instructions` | optional bool | Model self-report of text addressed to an AI/reader (see §8). |

`tax` is the **total** tax. If only component taxes are printed (CGST+SGST, state+county, …) the model sums them and says so in `extraction_notes`. Vendor tax IDs are compared after normalisation (case, spaces, hyphens, dots removed); the value itself stays as printed.

Not proposed (YAGNI): due date, payment terms, bill-to, bank details.

### 5.2 Evidenced-field additions (**set by the system, never by the model**)
`model_confidence: float | None` (the model's raw value) and `grounding: exact | normalized | fuzzy | not_found | value_mismatch | no_source | unavailable | None`. `confidence` becomes the **effective** confidence (raw, capped by grounding), so every existing rule and floor keeps working unchanged. These keys are not in the wire schema; if a model sends them anyway they are ignored and overwritten.

### 5.3 Wire format (what the model returns) - REVISED 2026-09-25
**SUPERSEDED AGAIN (2026-09-25): the per-field-object design below was also rejected ("compiled grammar is too large"); the header fields are now one array of entries and the API accepted it. See SPEC section 11, item 45.** Earlier revision: **The first design (nullable values via `anyOf`) was rejected by the API (49 union-typed parameters, limit 16). The wire schema now has zero unions/nulls/optionals: a `found` boolean per field with placeholders, and yes/no/unknown enums; it is converted back to the unchanged nullable contract. See SPEC section 11, item 44. The text below describes the original intent (money as strings, ISO dates).**

Money as **decimal strings** exactly as printed after separator normalisation (`"2160.00"`, never a float), dates as ISO strings, plus verbatim `source_text`. A schema-drift test asserts every `ExtractedInvoice` field exists in the wire schema and vice-versa.

### 5.4 `ExtractionMeta` / `IngestInfo` (system-side, `extra="forbid"`, on `RunContext`, in `meta.json` and audit events)
`path` (vision_only \| text_and_vision \| text_only), `mode_requested`, `pages_total`, `pages_processed`, `truncated`, per-page text-layer metrics, `model`, `prompt_version`, `attempts`, `schema_repair_used`, `degraded`, `failure_kind` (vendor_side \| system_side \| None), `failure_reason`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `request_id`, grounding summary counts, `injection_suspected`; ingest: `media_type`, `size_bytes`, `sha256`, `run_dir`, page image paths.

### 5.5 Touch points in M1 code (all covered by tests; need approval — Decision 2/3)
1. `RunContext` gains `ingest` and `extraction_meta`.
2. Engine floor gains reasons: `extraction_degraded`, `pages_truncated`, `reader_instructions_detected`.
3. `r_required_fields` returns `info` (not evaluable) when extraction failed **system-side**, so a failure of ours becomes `review`, not "ask the vendor" (see §10).
4. `resolve_vendor` uses `vendor_tax_id` first (`method="tax_id"`); tax id and name resolving to *different* vendors is reported as ambiguous with both as candidates.
5. `r_arithmetic`: expected total = subtotal + adjustments + tax.
6. New builtin rule `r_document_type` (13th): `invoice` passes; any other type flags severity 1; null is not evaluable.

## 6. Ingest stage

Input: a file path. Output: `IngestInfo` on the context, `ctx.file_hash`, a run folder, audit events.
1. **Reject (no run is created; clear error)**: empty file, unsupported type, over `max_file_bytes`. Type is decided by **magic bytes** (`%PDF-`, PNG, JPEG signatures), never the extension; a `.pdf` that is really an `.exe` is rejected.
2. **Hash**: SHA-256 of the original bytes (feeds `r_duplicate_exact`).
3. **Store**: copy to `data/runs/<run_id>/original.<ext>` (generated name; the user's file name is kept only as metadata, so path traversal or odd Unicode names cannot escape the folder).
4. **Render**: PDF pages via pypdfium2, images via Pillow (EXIF orientation applied, CMYK/palette/alpha → RGB), longest side ≤ `render_max_side_px`, PNG (JPEG if a PNG exceeds ~4.5 MB, the API's per-image limit). Page images are kept for the future UI.
5. **Text layer**: per-page embedded text and a *usable* flag (`≥ text_min_chars_per_page` and a word-like character ratio ≥ 0.6, no replacement-character garbage).
6. **Page cap**: pages beyond `max_pages` are not processed; `truncated=true` and the floor sends the run to review (Decision 5).
7. **Accepted-but-unreadable** (password-protected, corrupt, zero pages, all pages blank): the run continues with a degraded extraction (§10). No LLM call is made.

## 7. Extraction: combining embedded text and vision

Per document, mode `auto` (default):

| Situation | Path | What the model receives |
|---|---|---|
| Text layer usable (native PDFs) | `text_and_vision` | For each page: the page image **and** that page's text in `<page_text page="N">` delimiters |
| No/unusable text layer (scans, photos, image files) | `vision_only` | Page images only |
| `extraction_mode=text` (cost saver, optional) | `text_only` if usable, else vision | Text only |

Why both when possible: the image gives layout (which number belongs to which label, stamps, handwriting, table structure); the text layer gives exact characters, which vision can misread (0/O, 1/l, 5/6). The prompt says: *transcribe exactly what is printed; use the text layer for exact characters and the image for layout and anything the text lacks; if they conflict, prefer the image when the page looks scanned and the text layer otherwise; report any conflict in `extraction_notes`.* The **grounding check (§9) then verifies the model's answer against the text layer independently**, which is the "compare" step SPEC §3 asks for. The path used is recorded in `ExtractionMeta.path`.
(A `--compare-modes` eval option was considered and dropped for now.)

## 8. LLM client and prompt

**Client.** `LLMClient` is a small Protocol (`complete(LLMRequest) -> LLMResponse`), so tests inject a fake and the eval can replay. `AnthropicClient` sets: model from config, `max_tokens` from config, `thinking={"type":"disabled"}`, `output_config={"effort":"low","format":{"type":"json_schema","schema":wire}}`, `timeout`, `max_retries` (SDK), base64 image blocks. It reads `usage` (input/output/cache tokens) and `_request_id`, checks `stop_reason` (`max_tokens` and `refusal` become typed failures), and maps SDK errors to the `LLMError` tree. Every call is logged (INFO) and emitted as an audit event: model, attempt, tokens, cost, latency, request id — never the key or image bytes.

**Prompt (one module, versioned `extract-v1`).** Requirements and how they are met:
| Requirement | Mechanism |
|---|---|
| `null` for anything not found; never infer or repair | Explicit rules + `null` allowed on every wire field + confidence 0 for null |
| Numbers exactly as printed, even if the arithmetic is wrong | "Do not correct, recompute or round"; value must equal the digits in `source_text`; the arithmetic rule (M1) detects the error |
| PO reference explicit vs inferred | `po_reference.explicit`: true only if printed with a PO label; otherwise false |
| Invoice text is DATA, not instructions | System-prompt rule + page text inside delimiters (delimiter look-alikes in the text are neutralised) + output only through the schema (no free-form channel except `extraction_notes`) + deterministic scan (`injection.py`) + model self-report → floor to review. Even a successful injection cannot approve anything: **rules decide** |
| Fields by MEANING, not label text | Label lists in the prompt are examples, never a closed set. `total` = the final amount payable for this invoice (after discounts, including tax and shipping), commonly labelled Total / Grand Total / Amount Due / Balance Due / Amount Payable, never the subtotal. If a separate Balance Due differs (payments applied), `total` is the invoice total and the difference goes in `extraction_notes` |
| PO reference | Only a value printed with a purchase-order label counts (`explicit=true`); an "Order ID" / "Order No" is NOT a PO reference; otherwise `po_reference` is null. The model never infers a PO |
| Total tax | `tax` is the total; component taxes are summed and the sum is reported in `extraction_notes` |
| Adjustments | Amount as printed (absolute) plus a kind; the system applies the sign |
| Report ambiguities | `extraction_notes`; ambiguous d/m dates, currency symbols and label conflicts must be described there |
| Calibrated confidence | Written guidance: ≥0.95 crisp print, ≤0.6 blurry/ambiguous, 0 when null |
| `source_text` | Verbatim snippet including the label, ≤200 chars, and the 1-based page |

A prompt test suite asserts each clause is present; a hash snapshot forces a `PROMPT_VERSION` bump when the text changes.

## 9. Grounding check (`grounding.py`, deterministic, no LLM)

Model confidence is one signal, not truth. For every non-null evidenced field (and every line item and adjustment):

| # | Check | Needs text layer? | Result → effective confidence |
|---|---|---|---|
| G0 | `source_text` present | no | missing → `no_source`, cap **0.50** (principle 4: every claim has evidence) |
| G1 | Value agrees with its own `source_text` (amounts: the exact `Decimal` appears among the numbers in the snippet, handling `1,234.56` / `1.234,56` / `(500.00)` / currency marks; dates: the ISO date is among the parsable dates in the snippet, both d/m and m/d readings; identifiers/strings: normalised value inside normalised snippet) | no | disagree → `value_mismatch`, cap **0.30** — catches a model that "repaired" a number |
| G2 | `source_text` occurs in the page text | yes | verbatim → `exact`; after normalisation (case, whitespace, ligatures, Unicode minus, hyphenated line breaks) → `normalized`; close (edit similarity ≥ 0.90) → `fuzzy`, cap **0.75** |
| G2b | **Value fallback** (owner addition): many real PDFs have text layers whose reading order separates labels from values (all amounts in one block, then "Subtotal: Shipping: Total:"), so the full snippet often is not found even though the value is on the page. If the snippet is not found, check that the field's **value** is present in that page's text: amounts — the exact `Decimal` among the parsed numbers of the page (all plausible readings of `1,234.56` / `1.234,56` / Indian `1,00,000.00`); dates — the ISO date among the parsable dates; identifiers/strings — the normalised value | yes | value present → `value_present`, cap **0.85** (above the 0.8 review threshold); value absent from the page → `not_found`, cap **0.40** |
| G3 | Page repair | yes | If the snippet is found on a different page, the page number is corrected and noted |

`effective = min(model_confidence, cap_for_status)`; `exact`/`normalized` leave the model value untouched. With no usable text layer, G2 is `unavailable` (no penalty; G0/G1 still run). Caps are config values. Because the M1 floor already sends any required field below the confidence threshold (0.8) to review, an ungrounded total or invoice number can never be auto-approved. A summary line is appended to `extraction_notes` and the counts go to `ExtractionMeta` and the audit trail.
Fixture requirement: a text layer that lists the values before the labels must yield `value_present`, not `not_found`.
Not proposed: a second LLM "verification" pass (extra cost); revisit only if the eval shows grounding is not enough.

## 10. Failure modes

Principle: **never crash a run, never guess; degrade to an all-null extraction with a note.** Two kinds: *vendor-side* (the document is the problem → ask the vendor) and *system-side* (our problem → a human reviews).

| Situation | LLM called? | Outcome | `failure_kind` | Expected decision |
|---|---|---|---|---|
| Unsupported type / empty / oversize | no | rejected at ingest (no run) | – | clear error to the caller |
| **Password-protected**, **all pages blank** | no ($0) | all-null + note + `document_quality.issues` | vendor_side | `request_info` (via required fields) |
| **Corrupt / unrenderable** (renderer error, zero pages) | no ($0) | all-null + note + issues | **system_side** (our renderer may be the cause) | `review` |
| Page cap exceeded | yes (first N pages) | normal extraction, `truncated` + note | – | at least `review` (floor) |
| API key missing/empty | no | all-null + "LLM not configured" | system_side | `review` |
| Timeout, connection, 5xx, 429 after SDK retries | attempts logged | all-null + reason | system_side | `review` |
| 401/403/400 (auth, permission, bad request) | 1 attempt | all-null + clear reason | system_side | `review` |
| `stop_reason=max_tokens` / invalid JSON / schema-invalid | yes → **one repair retry** (previous error included) → | second failure → all-null + reason | system_side | `review` |
| `stop_reason=refusal` | 1 attempt | all-null + reason | system_side | `review` |
| Cost ceiling reached (run or session) | blocked *before* the call | all-null + "cost ceiling" | system_side | `review` |
| Reader-instructions detected (scan or self-report) | – | extraction kept, flag recorded | – | at least `review` (floor) |

Every row emits an audit event with the reason. SPEC §6.1 ("retry once, then mark the run `review` with the reason") is honoured for system-side failures.

## 11. Cost controls

1. **No reasoning spend:** thinking disabled, effort low, `max_tokens` capped (4096), page images capped (1568 px, `max_pages`), text capped per page.
2. **Ceilings** (config): per run ($0.25) and per session ($5.00). Before each call, `CostTracker` projects the worst case (estimated input tokens from image pixels ≈ w×h/750 and text length, plus `max_tokens` at the output price); if `spent + projection` exceeds a ceiling the call is not made (`CostCeilingExceeded`, clear message, run degrades to review; the eval script stops).
3. **Accounting:** exact `Decimal` cost from real `usage` (cache tokens included) via the config price table; per call logged and audited; totals into `ExtractionMeta` (and `runs.tokens_in/out/cost_usd/model` in M3); shown by the eval script.
4. **Estimate (to be measured in the first live run, not a promise):** a 2-page native invoice ≈ 9–10k input + 1–1.5k output tokens ≈ **$0.03**; a 20-invoice eval ≈ $0.60.
5. **Caching off by default** (system prompt is small and invoices arrive far apart; a cache write costs 1.25×). One config flag turns it on. The Batch API (50% off) is a future option for large evals, not needed now.

## 12. Tests

All non-live tests run offline, with the key blanked, using generated fixtures (reportlab PDFs, Pillow images, all created in `tmp_path`) and recorded/hand-written response JSON.

### 12.1 Mocked (default)
- **Ingest:** magic-byte sniffing (pdf/png/jpg; spoofed extensions; gif/txt/docx/exe rejected); empty/oversize; SHA-256 against known vectors; run-folder copy is byte-identical; hostile file names (`../x`, spaces, Unicode); same file twice → same hash, different run folders; page cap and truncation flag; password-protected, corrupt, zero-page, blank-page documents degrade with no LLM call; determinism.
- **Render/text layer:** size limits, EXIF rotation, CMYK/palette/alpha, huge images, page order, PNG→JPEG fallback; native vs scanned vs garbled text layer; per-page text mapping.
- **LLM layer:** request shape (model from config, thinking disabled, effort low, `format`, images base64, timeout); usage parsing incl. cache tokens; `max_tokens`/`refusal` handling; every SDK error class mapped; missing/empty key → `LLMConfigError` with a clear message; **canary key never appears in logs, exceptions, `repr`, meta or fixtures**; pricing exactness (`Decimal`), unknown model → `PriceNotConfigured`; ceilings: per run, per session (shared across runs), pre-call projection blocks the call, thread-safe accounting.
- **Prompt/wire:** every required clause present; version/hash snapshot; injection payloads in page text stay inside delimiters and cannot alter the system prompt; wire schema obeys API limits (`additionalProperties:false`, no constraints); wire↔`ExtractedInvoice` drift test; amounts stay exact strings/`Decimal`; unknown keys ignored and logged.
- **Extractor:** happy path from fixture; schema failure → one repair retry (corrective message contains the validation error) → success; two failures → degraded all-null, `system_side`; timeout/auth/refusal/ceiling → degraded with the right reason; vendor-side failures cost $0; path selection (text+vision vs vision-only vs text-only) verified in the request content; token/cost accounting; audit events.
- **Post-processing:** `$`→USD map, `€`/`£`, ambiguous symbols; d/m ambiguity policy; `1.234,56`, `(500.00)`, `€ 1 234,50`; sub-cent amounts preserved (M1 flags them).
- **Grounding:** exact / normalized / fuzzy / not-found; hallucinated total; "repaired" number (value ≠ snippet); wrong page repaired; ligatures, Unicode minus, hyphenated line breaks; no text layer; garbled text layer; caps configurable; model confidence lowered but never raised.
- **Injection:** each pattern; self-report; floor applies with all rules disabled.
- **Engine integration (M1 touch points):** system-side degrade → `review`; vendor-side → `request_info`; truncated → floor; tax-id resolution and tax-id/name conflict; adjustments in arithmetic; `r_document_type`; end-to-end (fixture file → ingest → extract with fake client → match → validate → decide) on hand-written facts; guardrail property tests re-run with the 13th rule.
- **Security:** `data/runs/` and `.env` gitignored; no key or image bytes in recorded fixtures or run metadata.

### 12.2 Live (`@pytest.mark.live`)
Skipped by default (`addopts = -m "not live"`) and skipped automatically when no key is set; run with `pytest -m live`. Cases: generated native PDF; generated scanned-style PNG; a prompt-injection page; asserts schema-valid output, key fields grounded, cost under a tiny per-test ceiling; also confirms the API accepts `thinking=disabled` + `effort=low`.

### 12.3 Eval script and answer key
`python -m app.extraction.eval [--dir data/invoices] [--manifest data/manifest.md] [--mode auto] [--replay DIR | --record DIR] [--max-cost USD] [--draft-manifest]` (no `--compare-modes` / `--json` for now)
- **Empty folder:** prints "0 invoices found in …" plus how to add files; exit 0. No key and no `--replay` → clear message before spending anything.
- **`data/manifest.md`:** human notes plus, per file, a heading and one fenced JSON block of expected values:
  ````
  ## acme_invoice_01.pdf
  - exercises: native PDF, tax excluded
  ```expected
  {"vendor_name":"Acme Supplies Ltd","invoice_number":"INV-1001","total":"2160.00","vendor_tax_id":null,
   "line_items":[{"description":"Widgets","amount":"2000.00"}]}
  ```
  ````
  Fields omitted are not scored; an explicit `null` means "must be null" (catches hallucination). Each entry carries `"verified": true|false`; **only `verified: true` entries are scored** and the report states how many files are unverified. `--draft-manifest` runs extraction and writes draft `expected` blocks into `data/manifest.md` marked `"verified": false`; **nothing is ever marked verified automatically** — a human flips the flag after checking.
- **Report:** per field — scored, correct, missed (null where a value existed), hallucinated (value where null expected), accuracy; money compared as exact `Decimal`, strings/identifiers after normalisation, dates exact; line items scored by exact amount + normalised description; mean confidence of correct vs wrong answers; grounding status counts; path counts; per-file failures; total tokens and cost. Unlabelled files are extracted but not scored; manifest entries without a file are warned about.

## 13. Build stages (each: tests run, committed, `STATUS.md` rewritten)

**Sequencing (owner):** build stages 1–3, commit, rewrite `STATUS.md`, then **stop** — the owner runs live checks first. By then `python -m app.extraction.cli <file>` prints the extracted JSON, the path used, tokens, cost and whatever grounding information exists at that stage; `python -m app.llm.probe` makes one tiny call and reports whether the API accepts `thinking=disabled` together with `effort=low` (the client falls back to `effort=low` only, and says so). Stage 4 starts only when the owner says so.

1. **LLM layer:** config, errors, client, pricing, budget, replay, key handling, test isolation fixture, live-test gating.
2. **Ingest:** validate, store, hash, render, text layer, ingest stage, generated fixtures.
3. **Contract + extractor:** contract additions, wire schema, prompts, post-processing, extractor with repair retry and degradation, extract stage.
4. **Grounding + injection + engine touch points** (§5.5). **DONE (2026-09-25)**; deviations from this plan are SPEC section 11 items 46-53 (fuzzy yields to value_present; missing tax checked as zero; unit-price rounding allowance; `po_found` also stands down after a system-side failure).
5. **Eval, manifest, CLI, live tests, SPEC §11 items.** **DONE (2026-09-25)**; the two existing `@pytest.mark.live` tests are unchanged and were not run (no live calls without the owner's say-so). The eval refuses live calls without `--live`; see SPEC section 11 items 54-57.

## 14. Risks

- Prompt quality is unproven until a live run: the offline suite proves plumbing, not accuracy. The eval on real invoices is the real test.
- Image token cost and the `disabled` + `low` combination are documented but unmeasured; stage 5 measures them.
- Native-PDF text layers of scanned documents (OCR'd by someone else) can be wrong; grounding treats absence as suspicious, not fatal.
- Whole-document processing has no chunking beyond the page cap.

## 15. Defaults I will use unless you object

Model `claude-sonnet-5`; thinking disabled + effort low; `max_output_tokens` 4096; SDK timeout 60 s / 2 retries; prompt caching off; structured outputs via `json_schema`; manifest as fenced JSON blocks; run folders `data/runs/<uuid>/`; page images PNG at ≤1568 px; max file 20 MB; grounding caps 0.50 / 0.30 / 0.75 / 0.40 (`no_source` / `value_mismatch` / `fuzzy` / `not_found`); reader-instruction scan on; test PDFs generated at test time (`reportlab`, dev-only).

## 16. Decisions (resolved 2026-09-25)

1. **Approved, extended:** any *explicitly stated* PO reference that is not an exact normalised match to the matched PO — including one that matches no PO at all — forces review (implemented; the known-gap test was replaced). A reference the extractor marked inferred (`explicit=false`) is exempt from the "matches nothing" cases.
2. **pypdfium2 + Pillow.**
3. **All contract additions and `r_document_type` approved**, with: adjustments returned as printed (absolute) + kind, sign applied by the system (discount/credit −, shipping/fee +, rounding/other as printed); `tax` = total tax (components summed, noted); ₹ and "Rs"/"Rs." → INR; the amount parser handles `1,234.56`, `1.234,56` and Indian `1,00,000.00`; tax IDs normalised (case, spaces, hyphens, dots) before comparison.
4. **Approved with one change:** password-protected and blank documents are vendor_side (`request_info`); corrupt/unrenderable files are system_side (`review`).
5. Approved (text + vision). 6. Approved (truncate at the page cap + review). 7. Approved (`$`→USD, plus ₹). 8. Approved (low-confidence reading for ambiguous dates). 9. Approved (cost defaults).
10. **Prompt additions (owner):** identify fields by meaning, not label text; total definition; Balance Due handling; an Order ID/Order No is not a PO reference (§8).
11. **Grounding addition (owner):** `value_present` fallback, cap 0.85 (§9).
12. **Eval (owner):** no `--compare-modes`/`--json`; add `--draft-manifest`; only `verified: true` entries are scored (§12.3).


---

# M3 plan: the pipeline (2026-09-25, awaiting owner approval; no M3 code exists)

**Goal.** One command takes a file path and runs ingest -> extract -> match -> validate -> decide -> explain -> act, persists everything to SQLite, and prints what happened. Reuses the M1/M2 machinery unchanged (`run_ingest_stage`, `run_extract_stage`, `load_facts`, `run_match_stage`, `load_rules`, `run_validate_stage`, `run_decide_stage`). Extraction and grounding logic are not touched. Out of scope: API/SSE/UI (M4), review resolution and dashboard (M5), the match-assistant and reviewer LLM roles (M7), any sending of email.

**Prerequisite (owner-gated, live, about $0.02-0.03).** The IQ Electronics scan is not in `data/invoices` (only the 5 SuperStore PDFs are there), so I need its path. Command once approved: `python -m app.extraction.cli "<path>" --record data\recordings`. I will report the extracted JSON and check: GSTIN in `vendor_tax_id`; CGST+SGST as a tax AMOUNT (not the 9% rate column); currency INR with no symbol on the page (the current symbol map cannot help there, so a wrong or null currency here would be a real finding, not something I would patch silently). The IQ vendor and PO in the seed are written only after seeing that extraction.

## 1. Module layout (new package `backend/app/pipeline/`)

```
pipeline/runner.py    run_pipeline(path, conn, client, settings, on_event=None) -> PipelineResult   (orchestration only)
pipeline/persist.py   ALL M3 SQL: AuditWriter, start_run, finish_run, save_invoice, commit_ledger, enqueue_review, save_draft (one transaction each)
pipeline/actions.py   plan_actions(ctx) -> ActionPlan (pure: decision -> which rows to write), then persist.py executes it
pipeline/digest.py    build_digest(ctx) -> TrailDigest: numbered facts F1..Fn taken ONLY from rule results, floors, match and extraction meta
pipeline/explain.py   explainer call + claim checker + deterministic template fallback
pipeline/draft.py     drafter call + checker + deterministic template fallback
pipeline/prompts.py   explainer and drafter system prompts, versioned and fingerprint-tested like extract-v3
pipeline/cli.py       python -m app.pipeline.cli <file> [...]
```
Config additions (all in `config.py`, none in code paths): `explainer_model` / `drafter_model` (default = `model_name`), `explainer_max_output_tokens` (700), `drafter_max_output_tokens` (900), `vendor_facing_rules` (which rule outcomes may be shown to a vendor), `demo_seed_path`, `explanation_max_sentences`. One `MeteredClient` and one `CostTracker` serve extraction, explainer and drafter, so the per-run $0.25 and per-session ceilings cover the whole run.

## 2. Flow, transactions, failure handling
1. `runs` row inserted (status `running`) and committed before any work, so a crash leaves a visible run.
2. ingest, extract: existing stages; their events are persisted right after each stage. An ingest rejection (unsupported/empty/oversize file) creates no run and prints the error, as in M2.
3. `load_facts(conn, run_id)` once (after extraction), match, validate, decide: events persisted per stage. Every event gets a monotonic `seq` from one `AuditWriter`.
4. explain (LLM, with template fallback), then act. The act stage runs in ONE `BEGIN IMMEDIATE` transaction: invoice, lines, ledger/review/draft rows, audit events for those actions. Any exception rolls the whole act stage back, marks the run `failed` and records a `pipeline_error` event; nothing partial is left.
5. `runs` finalised: `finished_at`, `final_decision`, summed tokens and cost, `model`; `final_decision` is never modified afterwards (SPEC section 5).
6. Extraction failures, missing key, timeouts, ceilings degrade exactly as in M2 and the run still gets a decision (review / request_info) with an explanation and, where applicable, a draft. The pipeline only fails on programming or database errors.

## 3. Exactly what is written, per table
| Table | Written when | Content |
|---|---|---|
| `runs` | every run | id, source_file (original name), status running -> completed/failed, started/finished, final_decision, tokens_in/out and cost_usd summed over extract + explain + draft, model (extraction model) |
| `audit_events` | every run | every event of every stage in order (ingest, extract incl. grounding/injection, match, validate: 13 rule results + engine_floor + engine_floor_reference + severity_aggregated, decide, explain incl. the explanation text, act incl. each row written); `detail` JSON-safe, no image bytes, no key. Roughly 35-45 rows per run |
| `invoices` | every run that reached extraction (also review/request_info/reject, so later duplicate checks see them) | run_id, vendor_id (resolved vendor, NULL if unresolved or ambiguous), number, date, currency, subtotal/tax/total in cents (NULL, with an audit note, if a value is missing or not representable), po_id (only when the match is MATCHED), decision, status (approve -> `approved`, review -> `in_review`, request_info -> `awaiting_info`, reject -> `rejected`), source_file, file_hash, `extracted` = the full ExtractedInvoice JSON (this is where adjustments live: there is no adjustments table and SPEC section 5 is not changed) |
| `invoice_lines` | with the invoice | line_no from 1, description, quantity/unit_price as decimal text, amount in cents |
| `ledger_entries` | approve with a matched PO only | one `commit` = the invoice total in cents; the PO balance stays derived (`get_po_balance`). The audit event records balance before and after |
| `purchase_orders.status` | with that commit | `partially_billed`, or `fully_billed` when derived balance <= 0 (never `closed`: that is a human action) |
| `review_queue` | review only | status `open`, `reason` = a deterministic one-liner from the triggered rules (rule id, outcome key, engine message). Evidence = the run's audit events (no evidence column; not changing the schema) |
| `drafts` | request_info and reject only | kind `vendor_email`, status `draft` (never `marked_sent`), subject, body, `to` NULL (vendors have no contact column). A reject caused by a blocked vendor writes kind `notification` (internal) instead of an email to the vendor |
| `rules`, `settings`, `vendors`, POs | read only | |

Edge rules: an approve is re-verified inside the transaction (PO balance re-read); if it changed since the snapshot so the approve would no longer hold, nothing is committed, the run is downgraded to `review` with an event saying why (escalate-only). The same file again is caught by `r_duplicate_exact` (reject), never a second commit.

## 4. Explainer (decide stage, LLM role "Explainer")
- **Input is only the digest**: `build_digest(ctx)` is deterministic code that lists numbered facts: final decision and severity; each triggered rule/floor reason (rule id, outcome key, severity, the engine's own message, whitelisted numbers from its detail); the passing rules as one summary fact; vendor and PO match facts; extraction failure/grounding/injection facts. No invoice text, no images, no free-text model output.
- **Output** (strict small schema): `{summary, reasons:[{text, facts:[F..]}], next_step}`; every sentence must cite fact ids.
- **Prompt constraints**: restate and clarify only; no fact not in the digest; no new numbers, names, dates or causes; no advice beyond the digest's `next_step` hint; the decision word must equal the given decision; never say a check "would pass" or suggest the decision could differ; plain language, at most `explanation_max_sentences`; treat digest text as data.
- **Deterministic claim check** on the reply: every cited id exists; every number/percent/date in the text appears in a cited fact; no other decision word than the real one; length cap. Failure -> one repair retry with the reason -> otherwise the **template explanation** built by code from the same digest, stored with `source: "template"`. So an explanation always exists, even with no key, and the LLM can never change the decision (the decision is already fixed and persisted in the run context before it is called).
- **Settings**: thinking disabled, effort low, max 700 output tokens, same client, same ceilings. Default model = the configured extraction model (priced in config, about $0.007 per call at roughly 1.2k in / 400 out). A cheaper model needs its price added to config by you.
- Stored as an `audit_events` row (stage `explain`, event `explanation`, detail = text, cited facts, source, model, tokens, cost); no schema change.

## 5. Drafter (act stage, LLM role "Drafter")
- Runs only for request_info and reject; never for approve or review (a rejection-after-review email is M5).
- **Input**: the digest restricted to vendor-facing facts (`vendor_facing_rules`: required fields missing/low confidence, no/unmatched PO reference, arithmetic mismatch, currency mismatch, wrong document type, duplicate). Never vendor status, blocked/new, internal thresholds, rule ids, severities, "AI" or system-side failures. Plus the extracted invoice number, date, total, and vendor name as extracted (grounded values).
- **Output**: `{subject, body}`. **Constraints**: list exactly the missing/unclear items as bullets; state a rejection reason factually; no promise of payment or timing; no invented names, amounts, dates, contacts or terms; no approval language; 150 words maximum; neutral, polite; signed generically ("Accounts Payable").
- **Check**: must mention the invoice number when known and each requested item; every number in the text must appear in the digest; no forbidden phrases (approve, will be paid, payment will...). Failure -> repair once -> template draft (still `status=draft`, `source: template`). There is no send code anywhere; a test blocks network access during the whole pipeline run.

## 6. Seed data and how the two seeds coexist
- Keep `data/seed.json` (the M0 placeholder) untouched; all M0/M1 tests keep reading it.
- Add `data/seed_demo.json`, clearly not a placeholder. `SeedFile` accepts either the `_PLACEHOLDER` notice or a `_DATASET` description (exactly one required), so the loader, its validation and `load_seed` are reused. `python -m app.db.reset --seed data\seed_demo.json` (and `--demo` as shorthand via `demo_seed_path`) loads it; a database holds one seed at a time, so ids never collide.
- Demo content: vendor 1 **SuperStore** (approved; no tax id, none is printed); five POs `PO-SS-001..005`, one per verified SuperStore invoice, with plain product-name lines from the verified manifest and totals above each invoice (e.g. 12,000 / 10,000 / 9,000 / 2,500 / 6,000); one PO partly consumed by a seeded historic approved invoice and ledger commit, so derived balances are visible; vendor 2 and one PO for **IQ Electronics** added after its extraction is seen. I checked offline that the 5 real invoices each match their own PO confidently against these five (top scores 0.57-0.60 vs the next 0.42-0.46), i.e. unambiguously but with a thin margin over the 0.50 minimum.
- **Seed rule (owner, 2026-09-25): PO line descriptions must resemble the real invoice line text, not be shortened.** Measured: the IQ line "APPLE IP 16 PRO MAX SL CS MGS PLM MYYW3Z Del.: 1801" scored 0 line overlap against a PO line "APPLE IP 16 PRO MAX" (similarity below the 0.6 minimum), which alone dropped a correct vendor+amount match under the 0.50 minimum. The demo seed will carry the full printed product text (without page furniture such as "Del.: 1801" only if the test shows it still matches).
- **Consequence to expect:** none of the five SuperStore invoices prints a PO number, so with this seed each goes to `review` (matched_without_reference), not approve. The approve path is shown by a labelled **controlled variant** (SPEC section 10): the same real invoice with `po_reference` set to the PO number by editing the recorded reply in the test, so the approve + ledger + PO-balance behaviour is exercised without a live call.

## 7. CLI
`python -m app.pipeline.cli <file> [--db PATH] [--reset-demo] [--replay DIR | --live [--record DIR]] [--max-cost USD] [--json]`. Like the eval, it **refuses to call the API unless `--live` is given** (exit 5, before any client exists). Prints in order: ingest, extraction (key fields, confidence, grounding), match (vendor, ranked POs with scores), validation (every rule: outcome, severity, message), decision, explanation (and its source), actions and drafts (full email text), a "written to the database" table (rows per table, PO balance before/after), then tokens and cost per stage. Exit codes: 0 done, 1 run failed, 2 usage or rejected file, 3 no key (live), 5 live refused.

## 8. Offline test list (no live calls; key blanked; `pytest -W error`)
- **Persistence:** one test per decision for exact rows in every table; audit `seq` monotonic and complete (count equals the sum of stage events; order ingest < extract < match < validate < decide < explain < act); run lifecycle and the failed-run path; `final_decision` never changes; minor-unit conversions, NULL handling, status mapping; act-stage rollback by fault injection leaves no partial rows; schema drift test that no stored PO balance exists.
- **Ledger:** approve commits once with the right cents; balance before/after via `get_po_balance`; PO status transitions; no commit for review/reject/request_info; same file twice -> reject, no second commit; balance changed during the run -> downgraded to review; a commit slightly over balance within tolerance.
- **Review/drafts:** review_queue row content; none for other decisions; drafts only for request_info/reject, always `draft`, blocked-vendor reject -> `notification`; a test that patches sockets to fail proves nothing is sent.
- **Explainer:** digest is deterministic and contains only trail facts (no invoice text or images in the request); claim checker rejects an unknown fact id, an unsupported number, a wrong or extra decision word, over-length; repair then template fallback; a reply that says "approve" for a review changes nothing; cost lands in the shared tracker; ceiling hit -> template; no key -> template; prompt clause tests and a fingerprint that forces a version bump.
- **Drafter:** vendor-facing whitelist (blocked/new status and internal ids never appear), required items all listed, forbidden phrases rejected, template fallback, word limit.
- **Seed:** demo seed parses and loads; placeholder seed untouched and still loads; both pass balance derivation; reset with `--seed`; the `_PLACEHOLDER`/`_DATASET` rule.
- **CLI:** every section printed; live refusal builds no client; replay works; exit codes; `--reset-demo` refuses a non-data path like `reset`.
- **Six-invoice end to end** (5 SuperStore recordings now, IQ Electronics once recorded) through the whole pipeline against the demo seed, with scripted explainer/drafter doubles that return valid, grounded replies plus the template path: expected decision table below and one-line reasoning per invoice in STATUS.md. Variants: PO referenced -> approve + ledger commit + partial-billed status; no PO -> request_info + draft; same file again -> reject + draft; blocked vendor -> reject + internal notification; degraded extraction -> review, no email.
- **Guardrails:** a property test that no LLM output can change a decision or write a ledger row; no vendor or file name appears in `app/`; determinism (same inputs and doubles -> identical events apart from timestamps).

**Honest limits.** The real explainer and drafter prompts have no live evidence until you approve a run (about $0.01 per invoice for both; about $0.07 for six). Offline, their quality is tested only through doubles, checkers and templates.

## 9. Build stages (commit after each; STATUS.md rewritten each time)
0. (owner-gated) live scan sample, then IQ vendor/PO facts.
1. `seed_demo.json` + loader relaxation + `persist.py` (audit writer, runs, invoices, lines) with tests.
2. `actions.py` + ledger/review/draft persistence + `runner.py` using template explanation/draft only; all decision paths tested.
3. `digest.py`, `explain.py`, `draft.py`, prompts, checkers, fallbacks.
4. `cli.py`, six-invoice end-to-end, STATUS.md with each invoice's decision and one-line reasoning; then stop.

## 10. Decisions needed from the owner
1. ~~IQ Electronics file path and the live scan run~~ **DONE** (2026-09-25): the scan is `data/invoices/image_based_invoice.jpg`, extracted live and re-recorded with extract-v4; see STATUS.md for what it showed (including an open currency finding).
2. Explainer/drafter model: default to the configured `claude-sonnet-5` (priced, cost negligible). Recommendation: yes; a cheaper model only if you add its price.
3. Explanation is stored as an `audit_events` row and review evidence is the run's audit events (no new columns; SPEC section 5 unchanged). Recommendation: yes.
4. Draft `to` stays NULL (no vendor contact data); a blocked-vendor reject makes an internal `notification`, not an email to the vendor. Recommendation: yes.
5. Ledger commit = full invoice total, which may leave a PO balance slightly negative when the tolerance rule allowed it (recorded in the event). Alternative: cap the commit. Recommendation: full total.
6. `data/seed_demo.json` beside the untouched placeholder, with the `_DATASET` loader relaxation. Recommendation: yes.
7. Approve path shown by a labelled controlled variant (PO number added by editing the recorded reply), since none of the six real invoices prints a PO number. Recommendation: yes.
8. Add the same `--live` guard to the existing M2 extraction CLI (today it calls the API whenever `--replay` is absent). This is CLI plumbing, not extraction logic. Recommendation: yes.

**M3 build status (2026-09-25):** stages 1-4 built as planned; deviations: the digest, templates and actions landed in stage 2 (with the runner) and the LLM roles in stage 3; the `runs` row is inserted right after a successful ingest (an ingest rejection creates no run); model roles are skipped when reader instructions are suspected; SPEC section 11 items 61-68. End-of-M3 re-record with extract-v5 done (2026-09-25, $0.143; see STATUS.md).


---

# M4 plan: API and live run view (2026-09-25; all 7 decisions approved as recommended; build stages 1-5, then stop)

**Goal.** Someone drops an invoice into the browser, watches each pipeline stage appear as it runs (status, key outputs, timing, expandable detail), and ends on a result view: the decision, the explanation, extracted fields with page and source text, every rule with its numbers, and what was written. Everything the view shows comes from `audit_events` and the other SQLite tables (SPEC section 1: "a live run view ... read[s] from that log"). A page refresh or a server restart therefore replays a run exactly. The pipeline logic is reused unchanged apart from the two small runner changes in section 2.

- Out of scope, M5: dashboard, review-queue actions, drafts "mark as sent", settings/rules editing, reset button.
- Out of scope, M7: the match-assistant and reviewer roles.

## 1. Module layout
```
backend/app/api/main.py      create_app(settings, llm_mode) -> FastAPI   (app factory; routes under /api)
backend/app/api/serve.py     python -m app.api.serve (--replay DIR | --live [--record DIR] | --offline) [--db PATH] [--port 8000]
backend/app/api/worker.py    RunWorker: one background thread, runs queued uploads one at a time, each with its own DB connection
backend/app/api/routes.py    the endpoints in section 3
backend/app/api/sse.py       event stream: tails audit_events by seq
backend/app/api/views.py     read models: DB rows -> JSON for the run view (read-only SQL, no pipeline objects)
frontend/                    React + Vite + TypeScript
  src/api.ts                 typed fetch + EventSource wrapper
  src/runState.ts            pure reducer: audit events -> per-stage view model (the one piece of logic in the UI; unit-tested)
  src/screens/Upload.tsx     drop zone, mode badge, recent runs
  src/screens/Run.tsx        live stage timeline; becomes the result view when the run finishes
  src/components/...         StageCard, DecisionBanner, FieldsTable (value, confidence, grounding, page, source text, page image),
                             RulesTable, WritesTable, DraftCard (read-only)
```
**Config additions** (`config.py`, env-overridable):
- `api_host` (127.0.0.1), `api_port` (8000)
- `api_cors_origins` (the Vite dev origin only)
- `api_upload_dir` (`data/uploads`, gitignored)
- `sse_poll_ms` (250), `sse_heartbeat_s` (15)

**New dependencies:**
- Backend: `uvicorn` and `python-multipart` (FastAPI needs it for uploads); dev: `httpx` (for FastAPI's TestClient).
- Frontend: `react`, `react-dom`, `vite`, `typescript`; dev: `vitest`, `@testing-library/react`.
- No UI component library and no state library.

## 2. Runner changes (the only change to M3 code)
1. `run_pipeline(..., run_id=None)`: the API picks the run id before the run starts, so it can return the id at once. The default is unchanged (generate one).
2. **Stage timing events.** For each of ingest, extract, match, validate, decide, explain and act, the runner writes two events:
   - `pipeline/stage_started`, committed BEFORE the stage runs, so the UI can show "running" during the 8-12 s extraction.
   - `pipeline/stage_completed`, whose `detail` holds the stage, status (ok / flagged / failed), `duration_ms` and a small `summary`.

   `summary` is built by one pure function per stage, from values the stage already produced, through a whitelist:
   - extract: path, fields found/total, low-confidence field names, grounding counts, tokens, cost
   - match: vendor and method, PO number and score, match status
   - validate: pass/flag/fail counts
   - decide: the decision
   - explain: source (template or model)
   - act: rows written

   No image bytes, page text, key or free-text model output go into it. This adds 14 events per run. The M3 tests that count or list events are updated; nothing else in them changes.

## 3. Endpoints (JSON unless stated; no auth; the server binds 127.0.0.1)
| Method, path | Does |
|---|---|
| `GET /api/health` | mode (`live` / `replay` / `offline`), model, session spend and ceiling, DB path, queue length |
| `POST /api/runs` (multipart `file`) | Streams the upload to `data/uploads/<uuid>/` and stops reading at 20 MB (413). Checks it with the existing `validate_file` (magic bytes, empty, size): a rejected file gets 415/400 with the ingest message and creates no run. Otherwise it queues the run and returns **202** `{run_id}` right away |
| `GET /api/runs?limit=20` | recent runs (id, file, status, decision, started, cost), for the upload screen |
| `GET /api/runs/{id}` | The run view: the run row, the invoice row and lines, extracted fields with evidence (from `invoices.extracted`), match candidates, rule results with their detail numbers, floor results, decision, explanation, drafts, review item, ledger entry with PO balance before/after, and timing and cost per stage. 404 for an unknown or malformed id (`check_run_id`) |
| `GET /api/runs/{id}/events` | **SSE.** Sends every audit event with `seq` > `Last-Event-ID` (the browser reconnects with that header automatically) as `id: seq`, `event: audit`, data = the stored row. Polls the table every `sse_poll_ms` with a fresh read and sends a comment line as a heartbeat. Once the run is no longer `running` and every event has gone out, it sends `event: end` `{status, decision}`. Stops polling when the client disconnects |
| `GET /api/runs/{id}/pages/{n}` | the rendered page PNG from the run folder, for evidence; served only from `pages/` of a valid run id, with n in range |

**Queueing and edge cases:**
- One worker thread, so runs execute one at a time in upload order. SQLite has one writer, and the approve re-check stays simple.
- A queued run has no `runs` row yet, so the stream sends `event: queued` until the row appears.
- If ingest still rejects a file in the worker (unexpected once `validate_file` has passed), the worker keeps the message in memory; the stream sends `event: rejected` and closes.
- Connections get `busy_timeout` 5 s. WAL is not needed at this scale: readers only wait for the short commits.

## 4. The live-call rule (same as the CLIs, SPEC item 68)
- `python -m app.api.serve` REFUSES to start unless given exactly one of `--live`, `--replay DIR` or `--offline`. With none it exits 5 and builds no client.
- `--offline` builds no client: extraction degrades to review and the explanation is a template. Handy for UI work.
- `--live` prints the same cost line as the CLIs. The UI shows a permanent **LIVE: paid API calls** badge (REPLAY or OFFLINE otherwise) and the session spend.
- The client is built once per server through the existing `build_client(... allow_live=...)`. A structural test keeps the API from building a client any other way.
- Ceilings: the per-run $0.25 and per-session $5.00 ceilings cover the server process's lifetime. Past $5, extraction degrades to review with the ceiling reason; a restart resets the counter.

## 5. UI (M4 screens)
- **Upload.** A drop zone (PDF/PNG/JPG, max 20 MB; a rejection is shown inline with the server's reason), the mode badge, and the last runs as a short list linking to their run views.
- **Live run view** (`/runs/:id`). A vertical timeline of the seven stages. Each goes waiting -> running (spinner, elapsed time) -> ok / flagged / failed, with its duration and a one-line summary from `stage_completed`. A card expands to that stage's events: message, outcome, and `detail` as readable key/value rows, not raw JSON. Rule results appear as they are written: pass/flag/fail with severity and the numbers.
- **Result view.** The same page once `end` arrives; it loads `GET /api/runs/{id}` and shows:
  - a decision banner (colour and word, never colour alone)
  - the explanation and its source (model or template)
  - the extracted fields: value, effective and model confidence, grounding status, page, source text; click to open the page image
  - the rule table
  - the match candidates with their score breakdown
  - what was written: ledger commit with PO balance before -> after, review item, draft email text (read-only, with "nothing is sent" stated)
  - tokens and cost per stage
- **Design.** Clean and restrained: system font stack, one accent colour, colour tokens with a dark theme, readable at laptop width and usable on a phone. Opening a finished run's URL shows the same result (the stream replays and ends immediately).

## 6. Tests (offline; key blanked; `pytest -W error`, plus `vitest`)
- **Runner:**
  - started/completed pairs for all seven stages, in order, with durations >= 0
  - `stage_started` is committed before its stage runs (checked from a second connection inside a slow fake stage)
  - the summary whitelist holds: no page text, image bytes, canary key or model free text
  - a `run_id` passed in is used
  - existing event tests updated
- **Upload:**
  - 202 and a run id for a good file
  - wrong type 415, empty 400, oversize 413 without reading past the limit, no file 422
  - a rejected upload creates no run and leaves no upload folder
  - hostile file names
- **Worker:**
  - two uploads run one after the other, in order
  - a pipeline exception ends as a failed run, and the stream ends with `status: failed`
- **SSE:**
  - every event exactly once, in seq order
  - resuming with `Last-Event-ID` sends only later events
  - `queued` before the run row exists; `end` after the last event
  - an unknown id is 404
  - heartbeat; a disconnect stops polling
  - a finished run replays and closes
- **Run view JSON:**
  - exact content for review, request_info, reject (blocked vendor -> notification) and the labelled synthetic approve variant (ledger commit, balance before/after)
  - all against the demo seed with the M3 doubles
  - amounts are exact decimal strings
- **Pages:** a valid page is served; traversal attempts, bad ids and out-of-range pages are 404.
- **Live rule:**
  - serve refuses to start without a mode (exit 5, no client); `--offline` builds no client
  - a canary key never appears in any response
  - CORS allows only the configured origin; the default host is 127.0.0.1
- **Frontend (vitest):**
  - the reducer on event streams recorded from the real backend in replay mode: a complete run, a resume halfway, a failed run, flagged stages
  - render tests for the stage card, decision banner and fields table from those same recordings
- **Manual, in Chrome, on replay:** upload the six real invoices one by one; screenshots of the live view mid-run and of each result, reported in STATUS.md.

## 7. Build stages (commit after each; STATUS.md rewritten each time)
1. Runner changes (section 2) and tests.
2. API: app factory, serve command with the live rule, upload and worker, run view, runs list, pages; tests.
3. SSE endpoint and tests.
4. Frontend: scaffold, upload screen, live run view, result view, reducer tests.
5. Browser check of the six on replay, STATUS.md; then stop. You then run it with `--live` on your own invoices, which is also the first real explainer/drafter output.

## 8. Decisions needed from the owner
1. **Server live rule:** `serve` requires `--live`, `--replay DIR` or `--offline` and has no default, matching the CLIs. Recommendation: yes.
2. **One run at a time** (a queue with one worker). Recommendation: yes. Parallel runs gain nothing in a demo and make SQLite writes and the approve re-check harder.
3. **Result view in M4** (SPEC 9.3), not M5: the live view ends in it, and you want to test real invoices with it. Recommendation: yes.
4. **`stage_started` / `stage_completed` events** in the audit trail: 14 more rows per run, carrying timing and a whitelisted summary. The alternative, keeping timing only in memory, would be lost on refresh. Recommendation: the events.
5. **Frontend stack:** React + Vite + TypeScript, hand-written CSS, no component or state library; vitest for the reducer and a few render tests. Recommendation: yes.
6. **Reset demo data** stays a CLI command (`python -m app.db.reset --demo`) in M4; the button comes with settings in M5. Recommendation: yes. Note: your own invoices, once uploaded, land in the demo database and in `data/runs` and `data/uploads` (both gitignored). Reset clears the tables but not those folders.
7. **Local only, no auth:** bound to 127.0.0.1, CORS for the Vite origin only. Hosting (SPEC 14, still pending) would need auth and is not planned here. Recommendation: yes.


---

# PO integration plan (2026-09-26; all 9 decisions approved as recommended; build stages 1-7, stop at 7)

**Where this sits.** Separate feature work, built before M5/M6 at the owner's priority; M5/M6 are not skipped. M4 stage 5 (the owner's manual browser check on replay) is still open: stage 5 is committed only on the owner's word, and nothing below changes M4 code before that.

**Goal.** Three ways to get a purchase order into the database (a form; free text drafted by the model; one uploaded document drafted by the model), all ending on the SAME confirmation form and the SAME save path. Invoices can be uploaded several at a time, each as its own independent run. A PO list and a PO detail view show each PO, its derived balance and every invoice run matched against it. Also a UI-only fix to the "Why" bullets.

**Guardrails (owner).**
- Matching is unchanged: SPEC section 5 and the engine are untouched. A new PO is simply one more row that `load_facts` already reads.
- No bulk multi-PO import: one PO per document.
- No path saves anything the user has not confirmed. The model only drafts; a person confirms by pressing Save on the form, and Save posts the values on the form, never the draft.
- Every endpoint that can call the model uses the server's one client, the cost ceilings, and the `--live` / `--replay` / `--offline` rule.

## 1. Data model: no schema change (recommended; decision 1)
The existing tables already cover the PO:
- `purchase_orders`: `po_number` (UNIQUE), `vendor_id` (NOT NULL), `currency` (NOT NULL), `total_amount` (cents), `issued_date`, `status`, `meta` (JSON).
- `po_lines`: `line_no`, `description`, `quantity`, `unit_price`, `amount`.

Provenance goes in `purchase_orders.meta`, which already exists and holds JSON:
- `source`: `manual` | `text` | `document`
- `entered_at`
- `draft_id`
- the free text as typed (text path, capped), or the file name, media type and SHA-256 (document path)
- model, prompt version, tokens and cost
- `edited_fields`: which fields the person changed from the model's draft

This needs no migration and no `PRAGMA user_version` bump, and SPEC section 5 is unchanged.

**What the schema forces, and how the form handles it:**
| Field | Required to save? | Why |
|---|---|---|
| vendor | yes | `vendor_id NOT NULL`; matching needs it |
| PO number | yes | UNIQUE; the reference matching looks for |
| currency | yes | `NOT NULL`; the currency check compares it. If the source does not state it, the field stays EMPTY and the person must choose it. The model never guesses and there is no default (decision 3) |
| total | yes | the amount the tolerance rule compares against |
| issued date, lines, line quantity / unit price | no | may be left empty (owner: never require what the source does not show) |
| status | not editable | a new PO is always `open`. Status is then derived from the ledger, and `closed` stays a human action on an existing PO (M5 settings) |

**Vendor.** The person either picks an existing vendor, or creates one inline (name, optional tax ID, optional country) with status **`new`**, never `approved` (decision 2). `r_vendor_status` then sends that vendor's invoices to review until someone approves the vendor, so a PO entered from an unknown document cannot open an auto-approve path. For drafts, the model's vendor name and tax ID go through the existing `resolve_vendor` (tax ID first, then name) to PRE-SELECT a suggestion. The suggestion shows the method and score, and the person confirms or changes it.

## 2. Backend layout (new package `backend/app/po/`)
```
po/models.py      POCreate (what Save posts), PODraft (what the model returns, per field: value, source_text, confidence), POIssue
po/validate.py    validate_po(create, conn) -> issues (deterministic; shared by all three paths)
po/store.py       save_po(conn, create, provenance) -> po_id   (ONE transaction; the only writer of POs)
po/wire.py        union-free wire schema (entries array, same shape as invoice wire v3) + from_wire
po/prompts.py     po-draft-v1: one system prompt, two input kinds (typed text / document); versioned and fingerprinted
po/drafter.py     draft_from_text(), draft_from_document(): client call, one repair retry, grounding, degrade -> PODraft
po/readers.py     document -> pages/text: PDF/PNG/JPG via the existing ingest; DOCX, XLSX, CSV text readers
po/views.py       PO list / PO detail read models (read-only SQL)
api/routes_po.py  the endpoints in section 4
```

**Shared code: all three paths converge on `POCreate -> validate_po -> save_po`.**
- **Form (manual):** the UI posts `POCreate`. No model, no draft.
- **Typed text:** `POST /api/pos/drafts/text` returns a `PODraft`. The UI pre-fills the SAME form with it, marks each field as "from the model" with its confidence and source quote, and the person edits and presses Save.
- **Document:** `POST /api/pos/drafts/document` returns a `PODraft`. Same form, same Save. The source quotes link to the rendered page (PDF/image) or show the text snippet (DOCX/XLSX/CSV).

`save_po` is the only function that writes `purchase_orders` / `po_lines`, and Save is the only endpoint that calls it. Draft endpoints write nothing to the database. They leave a draft folder at `data/po_drafts/<draft_id>/` (gitignored, like `data/runs`) holding the request metadata, the model reply, the draft and the cost, so the model's work is kept and auditable (decision 5). When Save carries a `draft_id`, the server diffs the confirmed values against that stored draft and writes `edited_fields` into `meta`.

**Reuse from extraction (not copied):**
- the LLM client and `MeteredClient`, and the cost tracker keyed by `draft_id`, so the per-run $0.25 ceiling applies per draft
- the amount, date and currency parsing and the symbol/name maps
- the grounding check, run against the typed text or the document text; it caps confidence exactly as for invoices
- the reader-instruction scan, the neutralised delimiters and `parse_reply`

The PO prompt and schema are separate from the invoice ones, so `extract-v5` and its recordings are untouched.

**Deterministic checks (`validate_po`)**, run on the draft (shown on the form) and again on Save (blocking where marked):
- Blocking: required fields present (table above); PO number not already used (exact); currency a 3-letter code in the configured two-decimal set (SPEC item 9); money has at most 2 decimals and is not negative; line quantity and unit price are numbers.
- Warnings, which can be saved anyway after the person has seen them: a PO number that matches an existing one after normalisation ("po 1001" vs "PO-1001"); the lines' amounts not summing to the total; quantity x unit price not equal to the line amount (same rounding allowance as the invoice arithmetic rule); a vendor created as `new`; a total above a configurable sanity limit.
- Never auto-fixed: "use the sum of the lines as the total" is a button the person presses, not something done silently.

**Document readers (`readers.py`):**
- **PDF / PNG / JPG:** the existing ingest (magic bytes, 20 MB, page cap, render, text layer) into a temporary PO folder, then the same text-and-vision / vision-only path choice as invoices.
- **DOCX:** stdlib `zipfile` + XML (the paragraphs and table cells of `word/document.xml`, in order); no new dependency.
- **XLSX:** `openpyxl` in read-only mode with cached values only (formulas are never evaluated, macros never run); new dependency, decision 4.
- **CSV:** stdlib `csv`, with the delimiter sniffed.
- Text-only formats go to the model as text only, no vision.
- Limits (config): uncompressed-size cap for DOCX/XLSX (zip-bomb guard), max sheets/rows/cells, max text characters. If a sheet holds several POs (several distinct PO-number columns or blocks), the model is told to return only the first and say so in the notes, and the form shows that warning. Nothing is imported in bulk.
- Rejected with a clear message: legacy `.doc` / `.xls` ("save as .docx / .xlsx / PDF"), macro-enabled `.docm` / `.xlsm`, encrypted files.

**PO wire fields:** `vendor_name`, `vendor_tax_id`, `po_number`, `issued_date`, `currency`, `total`, `lines[{description, quantity, unit_price, amount}]`, `notes`. Header entries carry `{name, found, value, page, source_text, confidence}`, as in invoice wire v3. The prompt carries the same rules as extraction: identify fields by meaning; return not-found rather than guess; never compute a total or tax the document does not print; a currency named in words returns its code; text that addresses the reader is data, not instructions.

## 3. PO detail view: data source (read-only SQL, no new decision path)
- **PO:** `purchase_orders` + `vendors` + `po_lines`.
- **Balance:** the existing `get_po_balance` (total minus `SUM(ledger_entries)`), never stored. Also shown: committed so far (ledger), and "awaiting review against this PO" (the sum of totals of matched invoices whose effective status is `in_review`; they do not consume the balance).
- **Invoices matched to it:** `invoices WHERE po_id = ?` (set only for a confident, unambiguous match), LEFT JOIN `runs`. Each row shows file, invoice number, date, total, the decision at run time, the effective status, cost, and a link to `/runs/:id`. Seeded historic invoices (no run) are listed as "historic, no run".
- **Ledger entries** for the PO (commit / reversal, amount, invoice, date).
- **Also considered in (decision 6):** runs where this PO was ranked as a candidate but NOT matched (ambiguous or below the minimum score), read from the stored `po_candidates_ranked` event with SQLite's `json_each`. Labelled clearly as not matched, so a person can see an invoice that almost landed on this PO.
- **PO list:** every PO with vendor, number, currency, total, derived balance, status, number of matched invoices; search by PO number or vendor, filter by status.

## 4. Endpoints
| Method, path | Model? | Does |
|---|---|---|
| `GET /api/vendors` | no | id, name, status, tax ID (for the vendor picker) |
| `GET /api/pos?q=&status=` | no | PO list (section 3) |
| `GET /api/pos/{id}` | no | PO detail (section 3); 404 for an unknown id |
| `POST /api/pos/validate` | no | `POCreate` in, issues out (the form calls it as the person types; nothing is saved) |
| `POST /api/pos` | no | **Save.** Body `{po: POCreate, new_vendor?: {...}, draft_id?: str}`. Blocking issues give 422 with per-field messages; an existing PO number gives 409; otherwise the vendor (if new) and the PO are written in one transaction and 201 `{po_id}` is returned. Nothing else creates a PO |
| `POST /api/pos/drafts/text` | yes | `{text}` (capped at `po_text_max_chars`) -> `PODraft` + issues + vendor suggestion. Writes nothing to the database |
| `POST /api/pos/drafts/document` | yes | multipart, one file -> `PODraft` + issues + vendor suggestion (+ page numbers for the viewer). Writes nothing to the database |
| `GET /api/pos/drafts/{draft_id}/pages/{n}` | no | a rendered page of the uploaded PO document |

**The live rule.** The draft endpoints use the server's single client:
- `--offline`: they answer 503 "offline mode: use the form".
- `--replay`: only recorded requests hit; a miss answers "no recorded response for this text/document; use the form".
- `--live`: they call the model under the same ceilings, and the UI's LIVE badge already warns about cost.
- A model failure never produces a half-filled form presented as complete: the reply says the draft failed and why (system-side codes only), and the person can still use the empty form.

**Calls:** synchronous, with a spinner in the UI. A PO draft is one call, about 10 s for a document. They do not go through the invoice worker queue, so an invoice batch does not delay PO entry (the tracker is thread-safe and the draft endpoints do not write to SQLite). **Cost:** a typed-text draft is about 2k tokens in / 0.6k out, roughly $0.01; a one-page document about $0.02-0.03, like an invoice.

**Multi-invoice upload: no backend change.** The browser posts each file to the existing `POST /api/runs`, one request per file, in the order chosen. Each becomes its own run in the existing one-at-a-time queue, with its own decision. A file rejected at upload (wrong type, too large, empty) fails alone, and the others still run. Config `ui_max_files_per_upload` (default 20) is enforced in the UI.

## 5. Frontend
- **Top navigation:** Invoices (upload + recent runs) | Purchase orders.
- **Upload screen:** accepts several files (drop or picker). A "This upload" list shows one row per file: waiting / uploading / queued / running / decided, with a decision chip and a link to each run's page. It polls `GET /api/runs/{id}` every ~1.5 s while any file is unfinished, instead of opening one EventSource per file (browsers cap open connections to one origin at about 6 over HTTP/1.1). Clicking a row opens the existing live run view.
- **PO list** (`/pos`): table with search and status filter, and a "New purchase order" button.
- **New PO** (`/pos/new`): three tabs (Form | Describe in text | Upload a document) that all lead to ONE `POForm` component.
  - Text and document tabs: submit, spinner, then the form pre-filled. Each model-filled field shows a "from the model" marker with confidence and the source quote (click to open the page for PDF/image). Empty fields are left empty and marked "not in the source".
  - Issues are shown inline (blocking in red, warnings in amber).
  - The vendor picker shows the suggestion and "create new vendor (status: new)".
  - The Save button reads "Save purchase order". Nothing is saved before it is pressed; leaving the page discards the draft (with a browser "unsaved changes" prompt when fields are dirty).
- **PO detail** (`/pos/:id`): header (number, vendor, status, currency), a balance card (total, committed, balance, awaiting review), lines, matched invoices (link to each run), ledger entries, "also considered in", provenance (source, entered, edited fields).
  - An "Upload invoices" button opens the same multi-file upload. Matching stays automatic, and the page says so: "invoices are matched automatically; an invoice that matches another PO or none is still processed and appears under its own result" (decision 7).
- **"Why" fix (UI-only, section 6).**

## 6. The "Why" bullets (UI-only; no backend change)
**Default view:** each bullet shows only the plain sentence. **Behind "Technical details"** (a small per-bullet expand): rule id, rule name, outcome code, severity and the cited fact ids.

**How the plain sentence is found:**
- The template writes each reason in one fixed form that our own code produces: `<rule_id> (<rule name>) - <outcome_key>, severity <n>: <message>`, or `Engine floor (<reason_code>): <message>`. The UI parses that prefix off and keeps `<message>`.
- Field identifiers inside the message (`vendor_name`, `invoice_date`, ...) are shown with the same labels as the fields table ("vendor name", "invoice date").
- A reason written by the model (the explainer) is already plain prose, and its cited facts go into the details.
- A reason in neither form is shown as it is, never hidden.

**Contract test:** a backend test that asserts the template reason format, without changing any backend code. It pins the format this parser relies on, so a wording change fails the test instead of silently breaking the UI. The same component is used for the review-queue reason in "What was written", which has the same raw form (decision 8).

## 7. Config additions
- `po_prompt_version`, `po_max_output_tokens` (1500)
- `po_text_max_chars` (8000)
- `po_doc_allowed` (pdf, png, jpg, docx, xlsx, csv)
- `po_zip_max_uncompressed_bytes` (50 MB), `po_sheet_max_cells` (20000), `po_doc_text_max_chars` (40000)
- `po_total_warning_above` (a sanity limit, default 10,000,000.00)
- `po_drafts_dir` (`data/po_drafts`)
- `ui_max_files_per_upload` (20)

## 8. Tests (offline; key blanked; fake/replay clients; `pytest -W error` + vitest)
- **Save path:**
  - one test per blocking rule (422 with the field named)
  - a duplicate PO number gives 409 and writes nothing
  - a warning-only PO saves
  - new-vendor create is atomic with the PO: a failure after the vendor insert rolls back both
  - a new vendor is always `new` (a request asking for `approved` is refused)
  - status is always `open`
  - cents conversion is exact; 3-decimal amounts are refused
  - `meta` provenance for all three sources; `edited_fields` computed from the stored draft; a `draft_id` that does not exist is 422
- **No silent save:**
  - the draft endpoints write NOTHING to the database (row counts of every table before/after), for success, repair and failure
  - a structural test: `save_po` is called from exactly one route, and no module in `app/po` or `app/api` other than `store.py` writes `purchase_orders` / `po_lines`
- **Matching unchanged:**
  - a PO saved through the form is picked up by the existing pipeline: a SuperStore invoice matches a newly entered PO with the same result as the seeded equivalent
  - the engine and matching test suites pass untouched
- **Drafter:**
  - request shape (model from config, thinking disabled, effort low, schema, text vs text+images)
  - wire drift test and union budget (same limits test as invoices)
  - repair retry then degrade; refusal, timeout and ceiling give a failed draft with a system-side code
  - grounding caps a value absent from the text
  - a currency not stated stays empty (no default)
  - a reader-instruction text is flagged and changes nothing
  - the cost goes into the shared tracker under the draft id
  - prompt clause tests and fingerprint snapshot
- **Readers:**
  - DOCX paragraphs + tables in order
  - XLSX cached values (a formula cell is never evaluated) with the cell cap
  - CSV with a sniffed delimiter
  - legacy `.doc` / `.xls`, macro-enabled, encrypted, zip bomb and oversize files are refused with a message
  - a sheet with two POs gives a first-PO draft plus the warning
  - all fixtures generated at test time (openpyxl / zipfile / reportlab), no real client documents
- **Live rule:** offline gives 503 without building any client; replay miss is a clear message; a canary key never appears in any PO response.
- **PO views:**
  - the list with derived balances; detail with lines, ledger, matched invoices
  - effective status vs decision; historic invoice without a run
  - "also considered in" from the candidate events, excluding the matched run
  - an unknown id is 404
  - the balance is never read from a stored column
- **Frontend (vitest):**
  - the Why parser on the real fixtures: template reasons, an engine-floor reason, a model-written reason, an unknown form; field ids shown as labels; details hidden by default and shown on expand
  - `POForm` pre-fill from a draft fixture with markers and empty "not in source" fields; blocking issues disable Save; "use sum of lines" only on click; vendor suggestion vs new vendor
  - multi-upload list states (one file rejected, others continue)
  - PO list and detail render from fixtures

## 9. Build stages (commit after each; STATUS.md rewritten each time)
1. The "Why" fix (UI) + the template-format contract test.
2. PO backend without a model: models, validate, store, vendors/PO list/detail/validate/save endpoints, views + tests.
3. PO frontend without a model: navigation, PO list, PO detail, the form (manual path) + tests.
4. PO drafter: wire schema, prompt, readers (+ `openpyxl`), the two draft endpoints with the live rule, draft folders + tests.
5. Frontend: text and document tabs on the shared form, source quotes and page viewer + tests.
6. Multi-invoice upload (UI) + upload from the PO detail page + tests.
7. Owner's manual browser check (as for M4 stage 5), then STATUS.md; then stop. The first real PO drafts need a live check you run yourself (about $0.01 per text draft, $0.02-0.03 per document); until then text/document drafting has only been exercised through fakes.

## 10. Decisions needed from the owner
1. **No schema change:** provenance and edit history live in `purchase_orders.meta`; SPEC section 5 is unchanged, with no migration. Recommendation: yes. The alternative is a `po_drafts` table plus `created_at` / `source` columns, which needs the first real migration.
2. **Inline new vendors get status `new`, never `approved`**, so their invoices go to review until a person approves the vendor (M5 settings). Recommendation: yes.
3. **Currency is required to save but never defaulted or guessed:** if the source does not state it, the person must choose it. Recommendation: yes (the schema requires it, and the currency check depends on it).
4. **`openpyxl` as a new dependency** for XLSX (read-only, cached values). DOCX and CSV use the standard library. Legacy `.doc` / `.xls` are refused. Recommendation: yes. Also accept PNG/JPG photos of a PO, which the existing ingest handles for free? Recommendation: yes.
5. **Drafts are files** (`data/po_drafts/<id>/`, gitignored), not database rows; the saved PO's `meta` points to its draft and records which fields the person changed. Recommendation: yes.
6. **"Also considered in" on the PO detail page** (runs where the PO was a candidate but not matched), clearly marked as not matched. Recommendation: yes. It is read-only and explains near-misses without touching matching.
7. **Uploading invoices from a PO page does not tell the matcher which PO to use.** Passing the PO as a hint would be new matching logic, which is excluded. The page states that matching is automatic. Recommendation: yes.
8. **Apply the same plain-sentence + technical-details treatment to the review-queue reason** shown in "What was written". Recommendation: yes; same component, same raw form.
9. **Editing or deleting an existing PO stays out of scope** here: changing a PO's total or lines after invoices matched it changes derived balances and past context. Recommendation: later, with M5 settings, as an audited action.


---

# Line-item PO consumption plan (2026-09-26; all 10 decisions approved as recommended; build stages 1-4, stop after 4)

**Where this sits.** Builds on PO integration (stages 1-6 committed; its stage 7 and M4 stage 5 are your open browser checks).

**Scope of THIS build:** schema, line matching, one new rule, and the stored data the reviewer's picker will need.

**Out of scope, the follow-up prompt:** the approve/reject endpoint, the picker UI, any new ledger-writing action.

**Owner decisions carried in (not re-opened):**
- **(1) Reviewer decides:** when line matching is not confident, a reviewer is shown the invoice line beside the PO's candidate lines and explicitly assigns it to a line or deducts it from the PO total unassigned.
- **(2) Two measures:** consumption is tracked by quantity AND amount per PO line.
- **(3) Price rule:** a new deterministic rule compares invoice-line vs PO-line unit price with a tolerance, separate from `r_arithmetic`.
- **(4) Whole-PO total matching stays:** a permanent, valid mode for invoices with no meaningful line correspondence.

**Principle kept:** money truth stays where it is. `ledger_entries` remains the only source of a PO's balance (total minus SUM), unchanged. The new consumption table is an ALLOCATION of each ledger entry: it records which PO line, what quantity and what amount. It never replaces or duplicates the balance calculation.

## 0. What the real data says (measured offline on the six recorded invoices, before planning)
- All six have exactly one line, and each PO in the demo seed has exactly one line. Quantities are equal, and unit prices are equal as decimals (1893.30 = 1893.3).
- **Expected result on the six:** every line is a confident single match, `r_po_line_price` passes, and no decision changes (five review, IQ review). The labelled synthetic approve variant still approves.
- **The existing description similarity (`token_similarity`) is too weak on its own for line-level choice:**
  - 24429 "Hon Rocking Chair, Black - Chairs, Furniture, FUR-CH-4682" vs its own PO line "Hon Rocking Chair, Black": **0.61** (the PO text is a shorter subset).
  - Two DIFFERENT fax machines, "Hewlett Fax Machine, Color ..." vs "Canon Wireless Fax, Laser ...": **0.70**.
  - "Hon Rocking Chair, Black" vs "..., Red": **0.82**.

  So the line matcher adds token containment and item codes (section 3). The whole-PO `lines_signal` used for PO ranking is NOT changed, so PO matching cannot regress.

## 1. Schema: version 1 -> 2 (SPEC section 5 addition; decision 7)
Two new tables. **No existing table or column changes.**

```sql
-- How each ledger entry is allocated: to a PO line, or to the PO total unassigned (po_line_id NULL).
-- Invariant: for every ledger entry, SUM(po_consumption.amount) = ledger_entries.amount (same sign convention).
CREATE TABLE po_consumption (
    id               INTEGER PRIMARY KEY,
    ledger_entry_id  INTEGER NOT NULL REFERENCES ledger_entries(id),
    po_id            INTEGER NOT NULL REFERENCES purchase_orders(id),
    po_line_id       INTEGER REFERENCES po_lines(id),          -- NULL = deducted from the PO total, no specific line
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    invoice_line_id  INTEGER REFERENCES invoice_lines(id),     -- NULL = the invoice as a whole (total-only)
    run_id           TEXT REFERENCES runs(id),                 -- NULL for seeded historic invoices
    quantity         TEXT,                                     -- decimal text, NULL when not known (total-only)
    amount           INTEGER NOT NULL,                         -- cents; > 0 commit, < 0 reversal (as the ledger)
    type             TEXT NOT NULL CHECK (type IN ('commit', 'reversal')),
    matched_by       TEXT NOT NULL CHECK (matched_by IN ('auto', 'manual_reviewer', 'legacy')),   -- 'legacy': decision 1
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((type = 'commit' AND amount > 0) OR (type = 'reversal' AND amount < 0)),
    CHECK (po_line_id IS NOT NULL OR quantity IS NULL)          -- a quantity only makes sense against a line
);
CREATE INDEX idx_consumption_po_line ON po_consumption(po_line_id);
CREATE INDEX idx_consumption_entry ON po_consumption(ledger_entry_id);

-- The line-matching result of a run, one row per invoice line, for the reviewer's picker (follow-up).
CREATE TABLE invoice_line_matches (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(id),
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    invoice_line_id  INTEGER NOT NULL UNIQUE REFERENCES invoice_lines(id),
    po_id            INTEGER NOT NULL REFERENCES purchase_orders(id),
    status           TEXT NOT NULL CHECK (status IN ('matched', 'ambiguous', 'no_match', 'not_evaluable')),
    po_line_id       INTEGER REFERENCES po_lines(id),          -- set only when status = matched (the automatic choice)
    score            REAL,                                      -- the top candidate's score (telemetry, like runs.cost_usd)
    candidates       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates)),   -- top 3, see section 3
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

The reviewer's later choice is NOT a column here: it becomes `po_consumption` rows with `matched_by = manual_reviewer` in the follow-up, so there is one record of what was consumed.

**How existing whole-PO commits map into the new model:** every existing `ledger_entries` row gets exactly ONE `po_consumption` row:

| Column | Value |
|---|---|
| `ledger_entry_id` | the entry |
| `po_id` | the entry's PO |
| `po_line_id`, `invoice_line_id`, `quantity` | NULL (unassigned, total-only) |
| `invoice_id` | the entry's invoice |
| `run_id` | the invoice's run, NULL if seeded |
| `amount` / `type` | copied from the entry |
| `matched_by` | `legacy` (decision 1) |

The PO balance is unchanged by construction. A PO line's remaining quantity and amount count ONLY line-assigned rows, so an old commit reduces the PO balance but no specific line. That is exactly what a total-only commit means.

Today this is one row in the demo seed (HIST-SS-0001, 1,500.00 on PO-SS-005), plus whatever approves exist in your `data/app.db`.

**Migration safety:**
- **An explicit command:** `python -m app.db.migrate [--db PATH]` (decision 8).
  1. Copies the database file to `app.db.v1-<timestamp>.bak`.
  2. In ONE transaction: creates the two tables, backfills one consumption row per ledger entry, verifies the invariant for every entry (count and sums), and sets `user_version = 2`.
  3. Any mismatch rolls back, and the file stays v1.
- Running it on a v2 database is a no-op.
- `init_db` knows versions 1 and 2. A fresh database gets schema v2 directly.
- The seed loader writes the matching legacy consumption row for every seeded ledger entry, through the same backfill function.
- `serve`, the pipeline CLI and the eval refuse a v1 database with a message naming the migrate command. `--reset-demo` also works, since it creates v2 fresh.
- `reset` drops all tables (it already drops every table in place); a test confirms the new ones go too.

**Keeping the invariant from now on (decision 2):** the existing approve path writes ONE total-only consumption row next to its ledger commit, in the same act transaction: `po_line_id` NULL, `matched_by = auto`, `run_id` and `invoice_id` set. Nothing new is decided and no new ledger action is added. The follow-up replaces this with per-line allocation.

`invoice_line_matches` rows are written in the act transaction, right after `invoice_lines` are saved (they need the invoice-line ids), whenever a PO was matched. This applies to every decision, because the reviewer needs them for review cases.

**Tests:**
- A v1 database built from the old schema.sql migrates with a byte-identical backup; the invariant and PO balances are identical before and after.
- A simulated failure mid-migration leaves v1 untouched; running it twice is idempotent.
- A fresh v2 database, the placeholder seed and the demo seed all satisfy the invariant.
- The CHECK constraints reject a quantity without a line, a wrong sign, and an unknown `matched_by`.
- `get_po_balance` is unchanged on every seeded PO.

## 2. Contract additions (SPEC 6.2 / 6.3; decision 7)
- **`POLineFact` gains:** `id`, `consumed_quantity`, `consumed_amount`.
  - The consumed values are the SUM of line-ASSIGNED consumption rows (commits minus reversals), loaded by `engine/loader.py`, which stays the only engine module that touches SQLite.
  - `remaining_quantity` = PO quantity minus consumed (None if the PO line has no quantity); `remaining_amount` likewise. Both are derived, never stored.
- **`RunContext` gains `line_matches: LineMatchSet | None`.** Frozen models:
  - `po_id`, `po_number`
  - `mode`: `line_level` | `total_only` | `partial` | `not_evaluable`
  - `bundled_hint: bool`
  - `lines: [InvoiceLineMatch]`, each with: `invoice_line_no` (1-based index into `extracted.line_items`), `status`, `po_line_id`, `po_line_no`, `score`, `candidates: [LineCandidate{po_line_id, po_line_no, score, breakdown{description, price, quantity, amount}, reasons}]` (top 3)
- **One new builtin rule** (section 4): 14 rules, so a validate run yields 16 results (14 + 2 floors).

## 3. The line matcher (`engine/line_matching.py`, deterministic, no LLM)
**When it runs:** in the existing match stage, after PO ranking, ONLY against the confidently matched PO (`ctx.matched_po`; decision 6).
- With no matched PO, or a PO without lines, `line_matches` is `not_evaluable` with the reason.
- An invoice line with no description is `not_evaluable`.

**Score for invoice line i against PO line j** (weights in a new `LineMatchConfig`, all config):

| Signal | Weight | Value (0..1) |
|---|---|---|
| description | **0.60** | `max(token_similarity, containment)`. Containment = the share of the SHORTER description's tokens found in the longer one, counted only when the shorter has at least `containment_min_tokens` (3) tokens. **Item codes:** a code token (letters+digits with a hyphen, or the invoice line's `item_code`) present in both gives 1.0 (`code:exact`); codes present on BOTH sides but different cap the description at `code_conflict_cap` (0.5), which separates two different products with similar names |
| unit price | **0.15** | 1.0 within the price rule's tolerance; otherwise linear from 1 down to 0 at `price_band` (25%) relative difference; 0.5 when either price is missing (unknown is neutral) |
| quantity | **0.15** | invoice qty <= the line's remaining qty: 1.0; over: `1 - excess/remaining` (floor 0); remaining <= 0: 0; either missing: 0.5 |
| amount | **0.10** | the same shape against the line's remaining amount |

**Why these weights.** The description decides the candidate. Price is deliberately a minor weight so a same-item line with a WRONG price still matches confidently; otherwise the price rule could never fire. For example, same description, price 10% high, quantity and amount fitting gives 0.60 + 0.09 + 0.15 + 0.10 = 0.94, which is confident and lets `r_po_line_price` flag it. Price and quantity still break ties between lookalike lines ("Black" vs "Red" chair at different prices).

**Candidates:** only PO lines with a description value >= `line_desc_min` (0.6, the existing config value), so amount alone never makes a candidate. Ranked by score, then PO line number.

**Per-line status:**
- `matched`: top >= `line_min_score` (0.75), AND either no runner-up, a runner-up below `line_ambiguity_min_score` (0.60), or a gap >= `line_ambiguity_margin` (0.10).
- `ambiguous`: top >= 0.75 with a close runner-up.
- `no_match`: no candidate, or top < 0.75.

**Several invoice lines on one PO line** (for example a split delivery): allowed. Invoice lines are processed in order, and each confident match reduces that PO line's remaining quantity and amount for the NEXT invoice line of the same invoice. So two lines that together exceed the ordered quantity score lower on the second (decision 9).

**Invoice-level `mode`:**
- `line_level`: every evaluable line is matched.
- `total_only`: no invoice line has ANY candidate, i.e. no meaningful correspondence, which is owner decision 4's permanent mode. Also `bundled_hint = true` when the invoice has one line and the PO has two or more.
- `partial`: anything else (some ambiguous or unmatched).
- `not_evaluable`: see above.

**This build does not change any decision on the basis of line matching** (decision 3). The result is stored for the reviewer's picker and read by the price rule.

**On the six real invoices** (computed above): descriptions 1.0 via containment for 24429, and 0.88-1.0 elsewhere. Price, quantity and amount are all 1.0, so every score is >= 0.93: `matched`, mode `line_level`.

**Stored:**
- `ctx.line_matches`.
- A match-stage audit event `po_lines_matched` (outcome info; the detail is the whole set, JSON-safe; summary counts go into the `stage_completed` summary).
- `invoice_line_matches` rows in the act transaction.

**Data for the picker** (read-only, no UI in this build):
- The run view JSON (`GET /api/runs/{id}`) gains `line_matches`: each invoice line with its status, the automatic choice, and its top-3 candidate PO lines, each with description, quantity, unit price, amount, remaining quantity and amount, score, breakdown and reasons.
- It also gains the PO's lines with consumed and remaining values, and the PO's unassigned (total-only) consumption, so the picker can offer "deduct from the PO total".
- The PO detail JSON gains per-line consumed/remaining quantity and amount, plus "consumed without a specific line".

## 4. The new rule: `r_po_line_price` (type `po_line_unit_price`)
- **Evaluated for:** every invoice line whose line match is `matched` (automatic, confident). In the follow-up, reviewer-assigned lines are included too.
- **Params**, the same pattern as `r_tolerance_pct` (decision 5):
  - `pct` (1.0), `abs` ("1.00"), `mode` (`lesser_of` | `greater_of`)
  - `direction` (`above` | `both`, default `above`; decision 4)
  - `severity_by_outcome`
- **Allowance** A = `min(PO unit price x pct/100, abs)` (`lesser_of`) or `max(...)` (`greater_of`), in Decimal. It is not rounded to cents, because unit prices may have more decimals. D = invoice unit price - PO unit price.
- **Outcomes:**
  - `within_tolerance` (pass): every compared line has |D| <= A, or D <= A with `direction = above`. A price BELOW the PO's is recorded in the detail with `direction = above`.
  - `price_above_po` (flag, severity 1): a line with D > A.
  - `price_below_po` (flag, severity 1, only with `direction = both`): a line with D < -A.
  - `not_evaluable` (info): no confidently matched line, or unit prices missing on every matched line. Per SPEC item 23, a missing essential input is info, never pass.
- **Severity 1 (review):** the same treatment as `r_tolerance_pct` (over tolerance -> 1).
- **Detail:** per compared line: invoice line no, PO line no, invoice unit price, PO unit price, D, relative difference, A, ok. Also which lines were skipped and why.
- **Distinct from `r_arithmetic`** (the invoice's own consistency) **and from `r_tolerance_pct`** (the total vs the PO balance). A test shows an invoice that passes both of those and fails this one.
- **Registry:** 14th builtin rule, source builtin, not locked, severity editable like the others; seeded with INSERT OR IGNORE, so an existing database gets it on the next init.
- **Existing assertions that count rules or results change:** 13 -> 14 rules, 15 -> 16 results per run, and the `rule_evaluated` count. They are listed in the stage-3 commit. No decision assertion changes. The recorded frontend fixtures keep their 15 recorded results.

## 5. Tests and fixtures (offline; `pytest -W error`; key blanked)
- **All six real invoices through the whole pipeline on the demo seed:**
  - line mode `line_level`, each line `matched` to line 1 of its PO, `r_po_line_price` pass
  - decisions identical to before (five review, IQ review); the synthetic approve variant still approves and writes the ledger commit plus one total-only consumption row
  - a before/after table in STATUS.md
- **New labelled SYNTHETIC fixtures.** Built on the generated Northwind invoice PDF already used by the M2 tests, with hand-written extraction replies and a test-only multi-line PO seed. The demo seed is not changed, so the six real runs are untouched.
  - **clean line-for-line:** invoice Widget A 10 x 60.00, Widget B 5 x 80.00 vs a PO with Widget A / B / C: both matched to the right lines, mode `line_level`, price pass
  - **ambiguous:** a PO with "Widget A (blue)" and "Widget A (green)", same price and quantity: `ambiguous`, both candidates listed, mode `partial`, decision unchanged
  - **unmatched / bundled:** invoice "Goods as per PO-5001", one line, 1,000.00, vs an itemised 3-line PO: `no_match`, mode `total_only`, `bundled_hint` true; the whole-PO match by reference is unaffected
  - **price mismatch:** invoice Widget A at 66.00 vs PO 60.00, quantities fitting: line `matched` (score about 0.94), `r_price_above_po` flag severity 1, decision review; the detail shows D = 6.00, A = 0.60
- **Line-matcher unit tests:**
  - containment vs similarity: the 24429 pair is 1.0; the two fax machines are separated by the code conflict
  - black vs red chair separated by price
  - remaining quantity from line-assigned consumption; legacy/total-only rows do not reduce a line
  - two invoice lines on one PO line reduce each other's remaining
  - a missing price or quantity is neutral
  - `not_evaluable` with no matched PO or PO without lines
  - determinism, and every weight from config
- **Rule tests:** each outcome; `lesser_of` / `greater_of`; `direction` both; not evaluable; the fail-safe (bad params -> flag at default severity, SPEC item 24); the guardrail property tests re-run with 14 rules.
- **Persistence:** `invoice_line_matches` rows for a matched PO, none without; act rollback leaves none; the run view and PO detail JSON carry the picker data; money as exact strings.
- **Migration and schema tests:** section 1.
- **The whole existing suite passes:** only the listed count assertions change.

## 6. Build stages (commit after each; STATUS.md rewritten each time)
1. Schema v2, `migrate` command with backup, backfill, invariant check, seed/reset/init support, the approve path's total-only consumption row. Existing suite green.
2. Facts (`POLineFact` id / consumed / remaining via the loader), `line_matching.py`, match-stage integration, audit event, `ctx.line_matches`, the six real invoices.
3. `r_po_line_price` in the registry; count-assertion updates listed; rule tests.
4. `invoice_line_matches` persistence, run view / PO detail picker data, the four synthetic fixtures, the six-invoice before/after table. Then stop for your review; the approve/reject wiring and the picker UI are the follow-up.

## 7. Decisions needed from the owner
1. **`matched_by` for migrated and seeded old commits.** Your enum is `auto | manual_reviewer`. The old whole-PO commits were either made by the system (`auto`) or are seed history that no matcher produced. Recommendation: add a third value **`legacy`** for all backfilled rows, so the record never claims an automatic match that did not happen. Alternative: map them to `auto`.
2. **The existing approve path writes one total-only consumption row** (`po_line_id` NULL, `matched_by auto`) beside its ledger commit, so the allocation invariant holds from this build on. Recommendation: yes. Without it, anything approved between this build and the follow-up would need a second backfill.
3. **Line-match confidence does not change any decision in this build.** A non-confident line match does not force review yet; the reviewer-choice flow of your decision 1 arrives with the follow-up, where we decide whether `partial` / `ambiguous` forces review. Recommendation: yes (this is what "no regression" requires). Consequence until then: an invoice with unmatched lines can still auto-approve on its total, exactly as today.
4. **Price rule direction:** flag only when the invoice price is ABOVE the PO price (`direction = above`, default); `both` is available as a param. Recommendation: `above`. A lower price favours the buyer, but it can signal the wrong item, and it is always recorded in the detail.
5. **Price tolerance defaults:** `pct` 1.0, `abs` 1.00, `lesser_of` (provisional, like the other thresholds). Recommendation: yes. Note that `r_tolerance_pct`'s 2% / 50.00 is sized for invoice totals, not unit prices.
6. **Line matching runs only against the confidently matched PO.** An ambiguous or unmatched PO gets no line matches; the reviewer picks the PO first. Recommendation: yes.
7. **SPEC changes** (section 5: two tables; section 6.2: `RunContext.line_matches`, `POLineFact` fields; section 6.3: a 14th builtin rule family). Recommendation: approve as written.
8. **Migration is an explicit command with an automatic backup;** `serve` and the CLIs refuse a v1 database and name the command. Recommendation: yes. The alternative is to migrate silently at startup, which is friendlier but touches your database without asking.
9. **Several invoice lines may consume the same PO line** (split deliveries), each reducing the remaining quantity and amount for the next. Recommendation: yes.
10. **The better description measure (containment + item codes) is used for LINE matching only;** the whole-PO `lines_signal` stays exactly as it is, so PO ranking cannot change. Recommendation: yes. Reusing it for PO ranking is possible later, with its own before/after check.


---

# Review actions + line allocation plan (2026-09-26; all 10 decisions approved as recommended; build stages 1-3, stop at 4)

**Where this sits.** Follows line-item consumption stages 1-4 (committed). It builds the review-queue part of M5 early: approve/reject, the queue screen and the approve flow. M5's dashboard, drafts "mark as sent" and settings are not in it.

**"As previously scoped" means**, since no review endpoint was ever planned in detail:
- SPEC section 8: a review item's approve/reject "writes to the ledger accordingly"
- section 9.5: open items with approve/reject controls
- section 11 item 12: `runs.final_decision` never changes; the human outcome goes in `review_queue.resolution` and `invoices.status`
- item 28: an approval commits the invoice TOTAL; PO status is derived from the ledger
- the M3 act stage's re-verification inside `BEGIN IMMEDIATE`

This plan turns those into exact contracts.

**Owner decisions carried in (not reopened):**
- **(1) Decide, then allocate.** Line-match confidence never changes the decision. Allocation happens after the decision, and only on approve.
- **(2) Automatic vs reviewer lines.** `matched` lines are allocated automatically. Every other line is shown to the reviewer with its ranked candidates plus "deduct from the PO total, no specific line".
- **(3) The one constraint on a manual assignment:** its amount must fit the chosen PO line's remaining amount, via the existing tolerance check applied per line. The description need not match.
- **(4) "No specific line" is permanent and legitimate.** The PO page shows it openly.
- **(5) Reject writes no allocation.**

## 1. Allocation: the rules (one pure function, `pipeline/allocation.py`)
`plan_allocation(invoice_lines, stored_line_matches, po_lines_now, ledger_amount, allocations_supplied, tolerance_params) -> AllocationPlan`

The same function builds the preview for `GET` and the rows written by `approve`, so what the reviewer sees is what gets written. It reads the STORED `invoice_line_matches` rows (no re-matching) and the PO lines' remaining amounts as of now.

**Per invoice line:**

| Line situation | Who allocates | Result |
|---|---|---|
| status `matched`, and its amount fits the matched PO line's remaining amount (per-line check below) | automatic | a consumption row on that PO line: `matched_by auto`, the invoice line's amount and quantity |
| status `matched` but it does NOT fit (the line was consumed meanwhile) | reviewer (decision 4) | shown with its candidates, the automatic choice pre-selected and marked "no longer fits" |
| status `ambiguous`, `no_match` (includes every line of a `total_only` invoice) | reviewer | shown with its stored ranked candidates; the top candidate pre-selected when it fits, otherwise "no specific line" pre-selected |
| no amount, or a zero amount | nobody (decision 6) | nothing to allocate; the value, if any, is part of the remainder; shown as a note |
| the PO has no described lines, or the invoice has no lines (`not_evaluable`) | automatic | everything goes to "no specific line"; there is nothing to choose, so no input is asked |

**Reviewer choices per line:**
- `po_line` with ANY line of the matched PO. It can be one of the ranked candidates or any other line of that PO; the description is not checked (owner decision 3). A line without an amount on the PO cannot be checked and is refused (decision 7).
- `unassigned`: a row with `po_line_id NULL`, `invoice_line_id` set, `quantity NULL`, `matched_by manual_reviewer`.

**The per-line fit check** (owner decision 3), reusing `engine/tolerance.evaluate_tolerance`:
- `B` = the PO line's remaining amount now (the line amount minus its line-assigned consumption), minus what EARLIER lines of this same approval assign to it (several invoice lines may land on one PO line).
- `I` = the invoice line amount.
- `pct`, `abs` and `mode` come from `r_tolerance_pct`'s CURRENT params in the database, so it is the same rule as the whole-PO check, "applied per line instead of per PO total".
- Fits when `I - B <= allowance`, with allowance = the lesser (or greater) of `pct`% of B and `abs`. With `B <= 0` the percentage part is 0, so in `lesser_of` nothing fits (as for the whole-PO rule, SPEC item 27).
- The error names the line, `I`, `B` and the allowance.

**The remainder (keeps the allocation invariant, SPEC item 74).** The ledger commit is the invoice TOTAL (item 28), but lines usually add up to the subtotal. Remainder R = total - sum(line allocations).
- R > 0 (tax, shipping, fees, rounding): one extra row against the PO total, `invoice_line_id NULL`, `matched_by auto`, described as "tax, shipping and other amounts not on a line".
- R < 0 (a discount or credit larger than tax + shipping): see decision 5.
- R = 0: no extra row.

The invariant is checked inside the transaction before commit (`consumption_problems` for that entry).

## 2. Endpoints (JSON; local, no auth; the reviewer is recorded as "reviewer (local UI)")

### `GET /api/review-queue?status=open|resolved&limit=` : the list
```json
{"items": [{"id": 7, "run_id": "...", "status": "open", "resolution": null, "resolved_at": null,
            "source_file": "invoice_Scot Wooten_10963.pdf", "invoice_number": "10963", "vendor": "SuperStore",
            "total": "5338.08", "currency": "USD", "po_number": "PO-SS-001", "reason": "Review: r_po_found (...): ...",
            "queued_at": "2026-09-26T10:01:00Z", "can_approve": true, "lines_needing_input": 0}]}
```
Open items are listed oldest first (a queue); resolved items newest first.

### `GET /api/review-queue/{id}` : one item, with the approve preview
```json
{"item": {"id": 7, "status": "open", "reason": "...", "run_id": "..."},
 "run": {"decision": "review", "explanation": {"...": "..."}, "source_file": "..."},
 "invoice": {"id": 2, "invoice_number": "10963", "total": "5338.08", "currency": "USD", "status": "in_review"},
 "approve": {
   "possible": true, "blocked_by": [],
   "warnings": [],
   "po": {"id": 1, "po_number": "PO-SS-001", "status": "open", "balance_before": "6000.00", "balance_after": "661.92"},
   "commit_amount": "5338.08",
   "state_token": "3f9c...",
   "tolerance": {"pct": 2.0, "abs": "50.00", "mode": "lesser_of"},
   "automatic": [{"invoice_line_id": 12, "invoice_line_no": 1, "po_line_id": 3, "po_line_no": 1, "amount": "5141.76", "quantity": "4"}],
   "needs_input": [],
   "remainder": {"amount": "196.32", "label": "tax, shipping and other amounts not on a line"}
 },
 "line_matches": {"...": "exactly GET /api/runs/{id}'s line_matches (reused, not recomputed)"}}
```

**A `needs_input` entry** (the picker's data; candidates come from the stored match, enriched from the run view's builder):
```json
{"invoice_line_id": 13, "invoice_line_no": 2, "description": "Widget A", "quantity": "10", "unit_price": "60.00", "amount": "600.00",
 "status": "ambiguous", "why": "two PO lines are about equally likely",
 "suggested": {"target": "po_line", "po_line_id": 8},
 "candidates": [{"po_line_id": 8, "po_line_no": 1, "description": "Widget A (blue)", "score": 0.857, "remaining_amount": "600.00",
                 "fits": true, "allowance": "12.00", "reasons": ["..."]}],
 "other_lines": [{"po_line_id": 10, "po_line_no": 3, "description": "Widget B", "remaining_amount": "400.00", "fits": false, "allowance": "8.00"}]}
```
The server supplies `fits` and `allowance` per option (computed with the same function) so the client check is the same check.

**Why approve can be blocked** (`possible: false`, `blocked_by` lists all that apply):
- the item is not open
- the invoice already has a ledger entry (a double approval)
- the run did not complete
- no PO was matched (decision 1)
- the PO is `closed`
- the invoice currency is missing or differs from the PO's
- the invoice total is missing, not positive, or not whole cents

A commit that goes over the PO balance is a WARNING, not a block: a human may approve what the tolerance rule held back, which is the one place severity may be lowered (SPEC principle 2). The balance after the commit is shown, negative if so.

### `POST /api/review-queue/{id}/approve`
Request:
```json
{"confirm": true,
 "state_token": "3f9c...",
 "allocations": [{"invoice_line_id": 13, "target": "po_line", "po_line_id": 8},
                 {"invoice_line_id": 14, "target": "unassigned"}],
 "note": "optional, up to 500 characters"}
```

Responses:

| Status | When | Body |
|---|---|---|
| **200** | done | `{"status": "approved", "ledger_entry_id", "amount", "po": {"po_number", "balance_before", "balance_after", "status"}, "allocations": [{"invoice_line_id", "po_line_id", "amount", "quantity", "matched_by"}], "remainder": {...}, "review_item": {"status": "resolved", "resolution": "approved"}}` |
| **422** `allocation_required` | a line that needs input has none | `{"error": "allocation_required", "message": "2 lines need a choice before this invoice can be approved.", "needs_input": [...as in GET...]}`. Nothing is written, and nothing is defaulted |
| **422** `allocation_invalid` | a supplied choice breaks a rule | `{"error": "allocation_invalid", "problems": [{"invoice_line_id": 13, "po_line_id": 8, "code": "exceeds_remaining", "message": "Line 2 (600.00) does not fit PO line 1: 250.00 remaining, allowance 5.00.", "amount": "600.00", "remaining": "250.00", "allowance": "5.00"}]}`. Codes: `exceeds_remaining`, `not_a_line_of_this_po`, `po_line_has_no_amount`, `automatic_line` (decision 3), `unknown_invoice_line`, `duplicate_line` |
| **409** `stale` | the PO's ledger or consumption changed since the preview (`state_token` differs), or the item was resolved meanwhile | `{"error": "stale", "message": "...", "preview": {...fresh approve preview...}}`. The UI shows the new numbers and asks again |
| **409** `not_approvable` | a `blocked_by` reason | the reasons |
| **400** | `confirm` is not `true` | |
| **404** | unknown item | |

**`state_token`:** SHA-256 of the PO's ledger rows (id, amount) and consumption rows (id, po_line_id, amount, quantity), plus the item status. It is taken by `GET` and compared inside the transaction.

**Inside ONE `BEGIN IMMEDIATE` transaction, in order:**
1. Re-read the item, invoice, run and PO; any `blocked_by` reason gives 409 with nothing written.
2. Compare `state_token` (409 `stale`).
3. Plan the allocation with the supplied choices (422 on a missing or invalid choice).
4. Ledger `commit` = invoice total on the PO.
5. The consumption rows: automatic lines, reviewer lines, the remainder. Verify the invariant for this entry.
6. PO status from the ledger (`partially_billed` / `fully_billed`; never `closed`).
7. `invoices.status = approved`.
8. `review_queue`: `status resolved`, `resolution approved`, `resolved_at`.
9. Audit events on the run: `review/human_approved` (reviewer, note, commit, balance before/after), one `review/allocation` event (every row, with `matched_by`), `review/review_resolved`. The seq continues after the run's last event.

Any exception rolls everything back. `runs.final_decision` stays `review` (item 12).

### `POST /api/review-queue/{id}/reject`
- **Request:** `{"confirm": true, "reason": "optional, up to 500 characters"}`
- **Checks:** the item is open and the invoice has no ledger entry (409 otherwise).
- **One transaction:** `invoices.status = rejected`; `review_queue` resolved/rejected; audit events `review/human_rejected` + `review/review_resolved`.
- **Never** a ledger entry or a consumption row (owner decision 5). No vendor email is drafted (decision 8).
- **Response 200:** `{"status": "rejected", "review_item": {...}}`

**No bulk:** there is no multi-item endpoint, every action needs `confirm: true` and one item id, and the UI offers no multi-select.

## 3. Frontend
- **Navigation:** Invoices | Purchase orders | **Review queue** (with the open count).
- **Queue screen (`/review`):** Open / Resolved tabs.
  - One row per item: file, vendor, invoice number, total, PO, the plain-language reason (the stage-1 parser), queued time, "needs line choices: 2" when relevant.
  - No checkboxes and no bulk action. A row opens the item.
- **Item screen (`/review/:id`):**
  - Left: the run's decision banner and "Why" (the existing components), key fields, and a link to the full run.
  - Right: the action panel.
  - **Approve, nothing to choose:** one panel with PO, commit amount, balance before -> after, the automatic allocations (invoice line -> PO line, amount), the remainder row, and warnings. Then one "Confirm approval" button.
  - **Approve, choices needed:** a card per line needing input, showing the invoice line (text, qty, price, amount) and why it needs a choice.
    - Options: the ranked candidates as radio buttons (score, PO line text, remaining amount, fits or not), "another line of this PO..." (a select of all its lines), and "No specific line (deduct from the PO total)".
    - The best guess is pre-selected.
    - Options that do not fit are shown but disabled, with the numbers ("600.00 does not fit: 250.00 remaining + 5.00 allowance").
    - The confirm button stays disabled until every line has a valid choice. The automatic lines and the remainder are listed above the cards so the reviewer sees the whole commit.
    - One submit sends everything.
    - A 409 `stale` replaces the panel with the fresh preview and says what changed; a 422 shows the server's problems on their lines.
  - **Reject:** an optional reason and a confirm step. It says plainly: "No ledger entry and no allocation will be written."
  - **After an action:** the result (commit, balance after, allocations, with links to the PO and the run) and "Next item" (the oldest open one).
- **Blocked items:** the approve panel shows `blocked_by` in plain words; reject stays available.
- **PO detail:**
  - The lines table gains "consumed" and "remaining" (quantity and amount) columns.
  - The stats row gains **"Consumed, not assigned to a line"** (`consumed_without_line`, already in the API), next to "Committed", with a one-line explanation.
  - The allocation rows are listed under Ledger (line or "no specific line", `matched_by`, invoice, run link).

## 4. Tests (offline; `pytest -W error` + vitest)
**Your five required tests:**
1. **All lines confident:** SuperStore 10963 (real, v5 reply) held for review, then approved with `allocations: []`. Expected: 200; one ledger commit of 5,338.08; one `auto` row on PO-SS-001 line 1 (5,141.76, quantity 4); one remainder row (196.32, no line); the invariant holds; PO `partially_billed`; invoice `approved`; item resolved; `final_decision` still `review`. Plus the synthetic clean scenario (two lines, both automatic).
2. **Mixed confident + ambiguous blocks until chosen:** the synthetic ambiguous scenario, first held for review.
   - Approve with no choice gives 422 `allocation_required`, listing exactly the ambiguous line with its candidates and the pre-selected guess.
   - Nothing is written: ledger, consumption, statuses and the item are unchanged.
   - Resubmitting with a choice for that line gives 200, with the automatic line `auto` and the chosen line `manual_reviewer`.
3. **An assignment over the remaining amount is refused clearly:** the target line was partly consumed first, so 422 `allocation_invalid` / `exceeds_remaining` with amount, remaining and allowance in the message, and nothing written. A choice exactly at remaining + allowance is accepted (boundary).
4. **Unassigned shows up and reduces no line:** approve with `target: unassigned`.
   - `GET /api/pos/{id}`: `consumed_without_line` rises by that amount (plus the remainder); every line's `remaining_amount` and `remaining_quantity` are unchanged.
   - The next run's line matching sees the same remaining amounts.
5. **Reject writes no allocation:** row counts of `ledger_entries` and `po_consumption` are unchanged; the invoice is `rejected`; the item is resolved/rejected; the audit events exist.

**Also:**
- **Allocation planner (pure):** every row of the section-1 table; several invoice lines on one PO line; any PO line allowed regardless of description; the remainder (positive, zero, negative per decision 5); a line without an amount; `not_evaluable` gives everything to the total; the fit check at B <= 0; `greater_of` / `lesser_of` from the rule's current params.
- **Re-verification:**
  - a stale token (another approval on the same PO in between) gives 409 with a fresh preview and nothing written
  - double approve and approve-after-reject give 409
  - a closed PO, a currency mismatch or a missing total give 409 `not_approvable`
  - a fault injected after the ledger insert rolls everything back
  - `confirm` missing gives 400
  - an unknown item or bad id gives 404
- **Validation codes:** `not_a_line_of_this_po`, `po_line_has_no_amount`, `automatic_line`, `unknown_invoice_line`, `duplicate_line`.
- **Audit and invariants:** the events and their order on the run; `consumption_problems` empty after every approve; `get_po_balance` equals total - ledger; `runs.final_decision` never changes.
- **List and detail:** ordering; `can_approve` / `lines_needing_input`; the detail's `line_matches` equals the run view's; resolved items.
- **Frontend (vitest, with fixtures recorded from these endpoints):**
  - the queue list; the one-click confirm path
  - the choices path: pre-selection, disabled non-fitting options with their numbers, confirm enabled only when complete, one submit with all choices
  - a 409 stale refresh; a 422 shown per line
  - the reject path's statement; the PO page's "consumed, not assigned to a line" figure and line columns
  - no bulk controls anywhere
- **Your manual browser check** at the end, as before.

## 5. Build stages (commit after each; STATUS.md rewritten each time)
1. `allocation.py` (planner + per-line fit + state token) and the two GET endpoints; tests.
2. Approve and reject endpoints (transaction, re-verification, ledger, consumption, statuses, audit); the five required tests and the rest of section 4's backend list.
3. Frontend: nav, queue, item screen, approve flow (both paths), reject, PO page figure and columns; vitest.
4. Your manual browser check; STATUS; then stop.

## 6. Decisions needed from the owner
1. **Review items with no matched PO** (for example an ambiguous PO match): approve needs a PO to commit against. **Recommendation (A):** approve is blocked for them in this build (`blocked_by: no matched PO`), and reject stays available. (B), letting the reviewer choose one of the run's ranked PO candidates and then allocating against it, needs line matching against a PO that was not matched. That is a real addition, better as its own step.
2. **Per-line check source:** use `r_tolerance_pct`'s current `pct` / `abs` / `mode` for the per-line allowance ("the existing tolerance check applied per line"). Recommendation: yes. The alternative is separate per-line params on a new setting.
3. **May the reviewer re-assign a line that was allocated automatically** (`status matched` and it fits)? **Recommendation: no, in this build** (422 `automatic_line`), per your decision 2. It keeps the automatic path free of reviewer input and the audit trail simple. It can be added later as an explicit "change" action.
4. **An automatic line that no longer fits** its PO line (consumed meanwhile) becomes a reviewer choice rather than over-allocating silently. Recommendation: yes.
5. **The remainder** between the invoice total (what the ledger commits) and the sum of its lines:
   - **positive** (tax, shipping, fees): one row against the PO total, labelled as such. Recommendation: yes.
   - **negative** (a discount or credit bigger than tax + shipping): the line allocations would add up to more than the commit. **Recommendation:** reduce the line allocations pro rata so they add up exactly to the commit (cents rounded down, the last cent on the largest line), recorded in the allocation event. The alternatives, blocking the approve or leaving a negative row, break the invariant or the sign rule.
6. **Invoice lines with no amount or a zero amount** are not allocated (nothing to consume); their value, if any, stays in the remainder, with a note in the approve panel. Recommendation: yes.
7. **A PO line with no amount** cannot be checked for fit, so a manual assignment to it is refused (`po_line_has_no_amount`) and the reviewer chooses another line or "no specific line". Recommendation: yes (strict, and consistent with "must fit").
8. **Reject after review drafts no vendor email** in this build. The reviewer's reason is recorded in the audit trail only; a draft-on-reject option belongs with M5's drafts screen. Recommendation: yes.
9. **Over the PO balance on a human approve** is a warning, not a block (the human may lower severity; SPEC principle 2). Only the conditions in `blocked_by` block. Recommendation: yes.
10. **The approve preview shows remaining amounts "as of now"** and the approve re-checks them inside the transaction via `state_token`. A change in between gives 409 and a fresh preview, never a silent recalculation. Recommendation: yes.


---

# Landing dashboard plan (2026-09-26; all 8 decisions approved as recommended; build stages 1-2, stop at 3)

**Goal.** Close SPEC section 9.4's "dashboard (history, status and outputs across runs)" with a landing page at `/`: a handful of stat cards and two short lists, all read from existing tables through existing query functions. No new decision logic and no new dependency.

**Out of scope:** charts from a library, filters/search (the Invoices and PO lists already have them), editing anything. The M5 items not yet built (drafts "mark as sent", settings/rules editing, reset button) stay open.

## 1. Backend: ONE read-only endpoint, `GET /api/dashboard?recent=8&review=5`
Built in `app/api/dashboard.py` from existing functions; the only new SQL is plain COUNT/SUM/GROUP BY aggregation over existing columns.

| Block | Source (existing unless marked) | Content |
|---|---|---|
| `runs` | new aggregate over `runs` | `processed` (status completed), `failed`, `running`, `by_decision` {approve, review, request_info, reject} from `runs.final_decision` (the system's decision at run time, never changed; SPEC item 12) |
| `outcomes` | new aggregate over `invoices.status` | the EFFECTIVE outcome now, after human review: {approved, in_review, awaiting_info, rejected, pending} (decision 2) |
| `review` | `review.service.open_count` + `review.service.list_items(conn, "open", N)` (reused as is) | open count and the oldest N open items (id, vendor, invoice number, total, PO, plain reason, `can_approve`, lines needing input) |
| `spend` | new SUM over `runs.cost_usd` + the existing PO draft files (`data/po_drafts/*/draft.json`, `provenance.cost_usd`) | `invoice_runs_usd`, `po_drafts_usd`, `total_usd`, `runs_counted`, `drafts_counted` (decision 4) |
| `pos` | `po.views.po_list(conn)` (reused: it already derives each balance from the ledger) | `count`, `by_status` {open, partially_billed, fully_billed, closed}, and **per currency**: `total_value`, `consumed` (= total - derived balance), `balance`, `consumed_without_line` (summed from `po_consumption_summary`, reused) (decision 3) |
| `recent_runs` | `api.views.recent_runs(conn, N)` (reused, extended with a LEFT JOIN to `invoices`/`vendors` for vendor name, invoice number, total and effective status: new fields only, so `/api/runs` gains them too; decision 8) | file, vendor, invoice number, total, system decision, current status, started, cost, link id |
| `generated_at` | clock | UTC time of the snapshot |

- **Money:** exact decimal strings (cents summed as integers); spend as dollar strings with 6 decimals, as runs record it.
- **Read-only:** one connection, reads only; a test checks that no table changes.
- **Performance:** fine at demo scale. `list_items` already builds each open item's preview; N is small.

## 2. Route and navigation
- `/` becomes the **Dashboard**. The upload + recent runs screen moves to **`/invoices`** (decision 1).
- Updated links:
  - the brand, "Back to upload" (404 page), "Upload an invoice" (run not found): the brand goes to `/`, the others to `/invoices`
  - "← New invoice" on the run page -> `/invoices`
  - the PO page's "Upload invoices" -> `/invoices?po=<id>` (the upload screen reads `po` from the query, as now)
  - the upload screen keyed by the query string, as now
- **Nav:** **Dashboard** | Invoices | Purchase orders | Review queue (count).
  - Dashboard is highlighted on `/`.
  - Invoices is highlighted on `/invoices` and on `/runs/:id`.
  - The others as now.
- An unknown path still shows "Page not found", with a link to the dashboard.

## 3. Frontend layout (`screens/Dashboard.tsx`; existing components and tokens only)
- **Header:** "Dashboard" plus one line ("Everything below is read from the audit log and the ledger; nothing here changes data"), and the snapshot time. It refreshes every 10 s while visible (the same pattern as the other lists).
- **Row 1, stat cards** (the existing `Stat` card, moved from `PODetail.tsx` into `components/common.tsx` for reuse, with no visual change):
  - **Invoices processed:** the count, with "N failed" underneath when N > 0.
  - **Decisions:** the four counts as chips (Approve / Review / Request info / Reject, the existing decision chips) plus a **dependency-free CSS proportion bar** (one flex row of four segments sized by share, each segment with a text label/title, never colour alone, `aria-hidden` bar + a text list). Below it one line: "Now, after review: X approved, Y rejected, Z still in review, W awaiting information".
  - **Review queue:** the open count, linked to `/review`.
  - **LLM spend:** the total, with "invoice runs $a · PO drafts $b" underneath.
- **Row 2, purchase orders:**
  - one card with the count and the status chips (open / partially billed / fully billed / closed)
  - one card PER CURRENCY: total value, consumed, balance, and "of which not assigned to a line"
  - the whole row links to `/pos`
- **Row 3, two short lists side by side** (stacked on narrow screens), using the existing run-list row style:
  - **Recent runs** (last 8): file, vendor, decision chip, current status if it differs ("review -> approved"), relative time; links to `/runs/:id`; "All invoices ->" links to `/invoices`.
  - **Waiting for review** (oldest 5 open): vendor, invoice, total, plain-language reason (the existing parser), "ready" / "N line choices" / "cannot approve"; links to `/review/:id`; "Whole queue ->".
- **Empty state:** with no runs, "No invoices yet: upload one" linking to `/invoices`; the PO block still shows the seeded POs.
- **Theme:** the existing tokens (light and dark), `section`/`stat`/`chip`/`run-list` classes; the only new CSS is the proportion bar (~15 lines) and the dashboard grid.

## 4. Tests
**Backend (`tests/api/test_dashboard.py`, offline, the demo seed + the scripted pipeline):**
- empty history: zeros everywhere, the seeded POs counted (6: 5 USD + 1 INR), per-currency totals equal to the seed, spend 0
- after runs (two SuperStore reviews, the labelled synthetic approve variant, a synthetic request_info, one failed run): processed 4, failed 1, the four decision counts, open reviews 2
- after approving one review item: `by_decision.review` unchanged (the system decision), `outcomes.approved` +1, open reviews -1, the PO's consumed and `consumed_without_line` per currency updated
- the PO block equals what `/api/pos` returns (same derived balances; one test sums `po_list` itself)
- spend = SUM(runs.cost_usd) + the draft files' costs (a scripted PO draft is made first)
- recent lists: order, limits, the `recent` / `review` params validated (1..50), links (ids) present
- read-only: every table's row count is unchanged by the call
- currencies are never added together (a USD and an INR PO give two entries)

**Frontend (vitest, with fixtures recorded from the endpoint):**
- `/` renders the dashboard and `/invoices` the upload screen
- the nav highlights Dashboard on `/`, and Invoices on `/invoices` and `/runs/:id`
- the cards show the fixture's numbers; the proportion bar has one segment per non-zero decision with text labels
- a per-currency card each; list rows link to `/runs/:id` and `/review/:id`
- the empty state; the PO page's "Upload invoices" link is `/invoices?po=<id>`

**Your manual browser check** at the end, as before.

## 5. Build stages (commit after each; STATUS.md rewritten each time)
1. The endpoint (+ the `recent_runs` join), backend tests.
2. The route/nav move, `Stat` moved into `common.tsx`, the dashboard screen, vitest.
3. Your manual browser check; STATUS; then stop.

## 6. Decisions needed from the owner
1. **The upload screen moves from `/` to `/invoices`**, and every link to it is updated. Old bookmarks of `/` now land on the dashboard. Recommendation: yes.
2. **Decision breakdown source:** the headline counts are the system's decision at run time (`runs.final_decision`, never changed), with one line underneath showing the effective outcome now after human review (`invoices.status`). Recommendation: show both; otherwise approved reviews would silently disappear from "review" or never appear as approved.
3. **PO money per currency, never summed across currencies** (the demo has USD and INR POs). Recommendation: yes; a single mixed total would be wrong.
4. **LLM spend** = invoice runs (`runs.cost_usd`) + PO drafts (their draft files; PO drafts are not runs). Shown as one total with the split. Recommendation: yes. The alternative, invoice runs only, would under-report once you draft POs live.
5. **Failed runs** are shown as a separate count, not inside the decision breakdown (they have no decision). Recommendation: yes.
6. **A CSS-only proportion bar** for the decision counts: no library, text labels always present. Recommendation: yes; trivial, and it reads faster than four numbers.
7. **List lengths:** 8 recent runs and 5 open review items, both query parameters. Recommendation: yes.
8. **`recent_runs` gains vendor, invoice number, total and current status** through a LEFT JOIN. These are new fields only, so `/api/runs` returns them too, and the upload screen's list can use them later. Recommendation: yes; it reuses the one existing runs query instead of writing a second one.


---

# Deployment plan: Render (backend) + Vercel (frontend) (2026-09-26, awaiting owner approval; no code for it exists)

**Goal.** Deployable configuration only. No business logic, test expectations or local workflow change: every local command in STATUS.md keeps working exactly as today, because every new setting defaults to today's value when its environment variable is unset.

**Out of scope:** new features; new dependencies (none needed: uvicorn, FastAPI and pydantic-settings are already there; Vercel builds with the existing Vite setup).

**The one real risk to decide first (decision 1).** The app was built local-only with NO authentication (M4 decision 7, SPEC item 70). Deployed publicly in `--live` mode, anyone who finds the URL could:
- upload invoices that spend your Anthropic key (the $5.00 session ceiling resets on every restart or deploy)
- approve or reject review items, which writes to the ledger
- create purchase orders

This plan therefore includes a minimal, opt-in access token (section 3). It is OFF when the variable is unset, so local development is unchanged.

## 1. What exists and is already environment-driven
Every `Settings` field is read from the environment (pydantic-settings, case-insensitive) and from `.env` locally:

| Setting | Env var | Today | Needed change |
|---|---|---|---|
| API key | `ANTHROPIC_API_KEY` | env / `.env` only, blank = unset, scrubbed from logs (SPEC item 42) | **none**: confirmed it stays env-only |
| database | `DB_PATH` | `<repo>/data/app.db` | none (or derived from `DATA_DIR`, below) |
| run folders | `RUNS_DIR` | `<repo>/data/runs` | same |
| upload temp copies | `API_UPLOAD_DIR` | `<repo>/data/uploads` | same |
| PO draft files | `PO_DRAFTS_DIR` | `<repo>/data/po_drafts` | same |
| CORS origins | `API_CORS_ORIGINS` | the two Vite dev origins | accept a comma-separated list as well as JSON (decision 5) |
| bind host / port | `API_HOST` / `API_PORT` | 127.0.0.1 / 8000 | none: the start command passes `--host 0.0.0.0 --port $PORT` |
| model, prices, ceilings | `MODEL_NAME`, `COST_CEILING_PER_RUN_USD`, `COST_CEILING_PER_SESSION_USD`, ... | config defaults | none |

## 2. Backend changes (small, config-level)
1. **`DATA_DIR` (decision 3).** When set, it becomes the default parent of the four writable paths: `DATA_DIR/app.db`, `DATA_DIR/runs`, `DATA_DIR/uploads`, `DATA_DIR/po_drafts`. Any of the four can still be set explicitly. Unset, nothing changes.
   - One setting pointing at the Render disk mount means no path can accidentally land on Render's ephemeral filesystem and vanish on the next deploy.
   - Implemented as a `model_validator` in `Settings` that fills only the fields NOT given explicitly.
2. **`SERVE_MODE` = `live` | `replay` | `offline` (decision 2).**
   - `python -m app.api.serve` reads it when no mode flag is given. A flag always wins. With neither, it still REFUSES to start (exit 5): the live-call rule of SPEC item 68 is unchanged, and the environment variable is just another explicit way to choose.
   - `replay` needs `REPLAY_DIR`. The recordings are gitignored (they may contain invoice data), so they are not on Render; in production the meaningful modes are `live` and `offline`.
   - Production (`render.yaml`) sets `SERVE_MODE=live`, as you asked.
3. **`GET /health`** (root, not under `/api`) for Render's health check.
   - Returns `{"status": "ok", "db": "ok", "schema_version": 2, "mode": "live"}` with 200. It opens the database read-only and runs `SELECT 1` plus a schema-version check.
   - Returns 503 with `{"status": "unavailable", "reason": ...}` when the database is missing, unreadable or the wrong version.
   - No path, no key, no spend. Exempt from the access token.
   - The existing `/api/health` (mode, spend, limits, DB path) stays as is, behind the token when one is set.
4. **Access token (decision 1; opt-in).** With `ACCESS_TOKEN` set:
   - every `/api/*` request needs `Authorization: Bearer <token>`, or `?access_token=<token>` for the EventSource stream and `<img>` page URLs, which cannot send headers
   - a missing or wrong token gets 401 `{"error": "unauthorized"}`, compared in constant time
   - `/health` is exempt
   - it is one small middleware in `main.py`, and CORS allows the `Authorization` header

   Unset (local), no check happens.
5. **CORS (decision 5):**
   - `API_CORS_ORIGINS` accepts `https://a.vercel.app,https://b.example` as well as a JSON list.
   - An optional `API_CORS_ORIGIN_REGEX` (unset by default) can admit Vercel preview URLs (`https://invoice-agent-.*\.vercel\.app`).
   - Locally nothing is set, so the two Vite dev origins stay the only ones, exactly as now. Methods GET/POST; headers `Content-Type`, `Last-Event-ID`, `Authorization`.
6. **Nothing ever resets or migrates automatically** (confirmed, and pinned by tests):
   - `serve` never calls `reset` or `init` on its own. Without `--reset-demo` (which `render.yaml` does NOT use) it only opens an existing database.
   - A missing database: refuses with the exact command to run.
   - A v1 database: refuses with the migrate command.
   - Missing builtin rules are INSERT-OR-IGNOREd, never overwriting (SPEC item 76).
   - First-time creation and demo seeding are one-time MANUAL commands in the Render Shell (DEPLOY.md, decision 4).
7. **Python version:** `render.yaml` pins `PYTHON_VERSION=3.12.7` (what the tests run on here).

## 3. Frontend changes
1. **`VITE_API_BASE`** (build-time):
   - `src/api.ts` prefixes every URL with `API_BASE = import.meta.env.VITE_API_BASE ?? ""`. That covers the fetch calls, the EventSource stream and the page-image URLs; all live in `api.ts`, and nothing outside it builds an API URL (checked).
   - Unset (local dev), it is `""`, so the URLs stay relative and Vite's `/api` proxy works exactly as now. On Vercel it is the Render URL, e.g. `https://invoice-agent-api.onrender.com`.
2. **Access token (only when the backend has one):**
   - On a 401, a small "Enter the access token" screen appears. The token is kept in `sessionStorage` for the browser tab and attached as `Authorization` (fetch) or `access_token` (EventSource, images).
   - It is never built into the bundle. Nothing changes locally, because the local backend never returns 401.
3. **`frontend/vercel.json`:** build `npm run build`, output `dist`, and an SPA fallback so a deep link like `/review/7` or a page refresh loads `index.html`:
```json
{
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "framework": "vite",
  "rewrites": [{ "source": "/((?!assets/).*)", "destination": "/index.html" }]
}
```
4. **`frontend/.env.example`** documents `VITE_API_BASE=` (empty = local proxy). The `.gitignore` already allows `.env.example`.

## 4. `render.yaml` (repository root; Render Blueprint)
```yaml
services:
  - type: web
    name: invoice-agent-api
    runtime: python
    plan: starter                      # a persistent disk requires a paid instance type
    buildCommand: pip install -e .     # editable: the app keeps finding data/seed_demo.json in the repo
    startCommand: python -m app.api.serve --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.7
      - key: SERVE_MODE
        value: live
      - key: DATA_DIR
        value: /var/data               # the disk below; the database, runs, uploads and PO drafts all live here
      - key: ANTHROPIC_API_KEY
        sync: false                    # set in the dashboard; never in this file
      - key: ACCESS_TOKEN
        sync: false                    # set in the dashboard (decision 1)
      - key: API_CORS_ORIGINS
        sync: false                    # your Vercel URL, set in the dashboard after the frontend exists
    disk:
      name: invoice-agent-data
      mountPath: /var/data
      sizeGB: 1
```
- There is no `--reset-demo`, `init` or `migrate` anywhere in it. A test fails if one ever appears.
- A disk means one instance and a short downtime on each deploy (Render's rule for disks), which is fine for this app.

## 5. `DEPLOY.md` outline (exact dashboard steps)
1. **Before you start:** a Render account (paid instance type for the disk), a Vercel account, the repo pushed to GitHub, your Anthropic key, and a long random `ACCESS_TOKEN` (a command to generate one is given).
2. **Render, backend:**
   1. New > Blueprint, pick the repo; Render reads `render.yaml`.
   2. Fill the `sync: false` values: `ANTHROPIC_API_KEY`, `ACCESS_TOKEN`; leave `API_CORS_ORIGINS` for step 4.
   3. Deploy. The first start REFUSES on purpose ("the database does not exist") and the health check fails.
   4. Render Shell, run ONE of:
      - `python -m app.db.reset --demo --db /var/data/app.db` (the demo dataset)
      - `python -m app.db.init_db --db /var/data/app.db` (empty: schema + rules only)
   5. Manual Deploy > Restart; `/health` turns green.
   6. Copy the service URL (`https://<name>.onrender.com`).
3. **Vercel, frontend:**
   1. New Project, the repo, **Root Directory `frontend`** (Vite is detected; `vercel.json` sets the rest).
   2. Environment variable `VITE_API_BASE` = the Render URL (no trailing slash), for Production (and Preview if wanted).
   3. Deploy; copy the Vercel URL.
4. **Back to Render:** set `API_CORS_ORIGINS` = the Vercel URL (comma-separate several). Optionally `API_CORS_ORIGIN_REGEX` for preview deployments. Save; Render restarts.
5. **Check:**
   - open the Vercel URL and enter the access token
   - the dashboard loads and the LIVE badge shows
   - upload one invoice (about $0.03)
   - `https://<render>/health` returns ok
6. **Operating it:**
   - the database lives only on the disk, and deploys never touch it
   - resetting is manual and destructive (the Shell command, with a warning)
   - migrating is manual (`python -m app.db.migrate --db /var/data/app.db`)
   - backups: download `/var/data/app.db` from the Shell, or Render disk snapshots
   - the session cost ceiling ($5.00) resets on every restart, so watch the spend on the dashboard
   - rotating the token = change the env var (tabs are asked again)
7. **Local development is unchanged:** the two-window commands in STATUS.md; no env var needed.

## 6. Tests (offline)
**Backend:**
- `DATA_DIR` derives the four paths, explicit ones win, unset keeps today's paths
- `SERVE_MODE` starts in that mode, a flag overrides it, neither refuses with exit 5, `replay` without `REPLAY_DIR` is a usage error
- `/health`: 200 with a good database; 503 for missing / wrong version; no path or key in the body; reachable without a token
- CORS: comma list, JSON list, regex, and the local default unchanged
- access token:
  - with it set: 401 without it, 200 with header or query, wrong token 401, `/health` exempt, SSE works with `?access_token=`
  - with it unset: everything as today (the whole existing suite runs with it unset)
- `serve` never resets: a missing DB refuses and no file is created
- `render.yaml` parses, its start command has no reset/init/migrate, and its secrets are `sync: false`

**Frontend:**
- `API_BASE` empty gives relative URLs (today's behaviour); set gives prefixed fetch, EventSource and image URLs
- a 401 shows the token screen, the token is attached afterwards, and it is stored in `sessionStorage` only
- `vercel.json` has the SPA rewrite

**Regression:** the full backend (2142) and frontend (79) suites unchanged.

## 7. Build stages (commit after each; STATUS.md rewritten each time)
1. Backend config: `DATA_DIR`, `SERVE_MODE`, CORS list/regex, `/health`, access-token middleware (off by default) + tests.
2. Frontend: `VITE_API_BASE`, token screen + tests; `vercel.json`, `.env.example`.
3. `render.yaml`, `DEPLOY.md`, the config tests; a local "production-like" smoke run (`SERVE_MODE=offline DATA_DIR=<scratch> ACCESS_TOKEN=x`, token checked, `/health`, CORS). Then stop: the actual Render/Vercel setup is yours, following DEPLOY.md.

## 8. Decisions needed from the owner
1. **Access protection for the public deployment.**
   - (A) The opt-in `ACCESS_TOKEN` of sections 2.4 and 3.2. **Recommendation.**
   - (B) No protection: anyone with the URL can spend your key and change the ledger. Not recommended.
   - (C) Deploy publicly in `offline` mode only (no spend, but also no real extraction) until (A) exists.

   With (A), the production default `SERVE_MODE=live` is reasonable.
2. **`SERVE_MODE` environment variable** (flags win; neither = refuse, as today). Recommendation: yes.
3. **`DATA_DIR`** as the single switch for all writable paths, rather than setting four variables separately. Recommendation: yes; it removes the "one path forgot the disk" failure.
4. **First-time database creation is a manual Render Shell command** (demo seed or empty); `serve` refuses a missing database and never creates, resets or migrates one on its own. Recommendation: yes, as you asked. The alternative, a `--create-if-missing` flag that creates an empty schema only when no file exists, is safe but still automatic.
5. **CORS:** `API_CORS_ORIGINS` also accepts a comma-separated list, and an optional `API_CORS_ORIGIN_REGEX` admits Vercel preview URLs (off unless set). Recommendation: yes.
6. **`/health` minimal and public** (status, database reachable, schema version, mode). `/api/health` keeps its details, behind the token. Recommendation: yes.
7. **A paid Render instance type is needed for the disk.** Render's free web services have no persistent disk, so the SQLite database would be wiped on every deploy or restart. Recommendation: `starter` (or larger).
8. **Cost exposure:** the $5.00 session ceiling is per server process and resets on restarts or deploys. For a public deployment you may want a lower `COST_CEILING_PER_SESSION_USD` (an env var, no code). Recommendation: set it to what you are comfortable losing between restarts, e.g. 2.00.
