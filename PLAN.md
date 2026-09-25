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
