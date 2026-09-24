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
**The first design (nullable values via `anyOf`) was rejected by the API (49 union-typed parameters, limit 16). The wire schema now has zero unions/nulls/optionals: a `found` boolean per field with placeholders, and yes/no/unknown enums; it is converted back to the unchanged nullable contract. See SPEC section 11, item 44. The text below describes the original intent (money as strings, ISO dates).**

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
4. **Grounding + injection + engine touch points** (§5.5).
5. **Eval, manifest, CLI, live tests, SPEC §11 items.**

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
