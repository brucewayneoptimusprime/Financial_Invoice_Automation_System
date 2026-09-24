# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 done. **Stage 4 (grounding, injection scan, engine touch points, real-invoice end-to-end) is done and committed.** Stage 5 (eval) is next; no live API call was made in this stage.

## Test count and result
**1500 passed, 0 failed, 2 deselected** (`pytest -W error`, ~39 s; +210). 23 mutation checks (grounding raising confidence, no `value_present`, no line/adjustment grounding, no symbol confidence, adjustments ignored, each floor removed, tax-id ignored, etc.): all killed (one survivor found and fixed with a new test).

## What changed
- **Grounding** (`extraction/grounding.py`): every field, line and adjustment gets a status and a capped confidence, never raised. On both real PDFs the label-separated values come out **`value_present` at 0.85** (subtotal, total, shipping, discount, and 10963's date); nothing is `not_found`. `$` currency: effective 0.85 from config, raw 0.7/0.8 kept in `model_confidence`, note added.
- **Engine:** `r_arithmetic` adds signed adjustments; tax-id vendor resolution; `r_document_type` (13th rule); the floor gains `extraction_degraded`, `pages_truncated`, `reader_instructions_detected`; a system-side failure is now `review` (`r_required_fields` and `r_po_found` stand down), vendor-side stays `request_info`.
- **Injection scan** (`extraction/injection.py`, config patterns) + the model's self-report -> `injection_suspected` -> floor.
- **CLI** now prints grounding counts and a reader-instruction warning.
- **Fixtures:** the two real replies and PDFs are in `backend/tests/fixtures/real/`; `tests/test_real_invoices_e2e.py` runs both through ingest, extraction (played back), grounding, match, validate, decide.

## Real invoices end to end (hand-written facts: SuperStore vendor, PO-SS-1 with plenty of balance)
| Case (both 10963 and 24429) | Decision | Triggered |
|---|---|---|
| Matching PO | **approve** | nothing |
| No PO | **request_info** | `r_po_found` no_reference (sev 2), `engine_floor` no_po_candidates (sev 1) |

Full trail, matching PO (identical for both, all 13 rules + 2 floors pass): r_arithmetic consistent, r_currency_mismatch match, r_document_type allowed, r_duplicate_exact no_duplicate, r_duplicate_fuzzy no_near_duplicate, r_extraction_confidence confident, r_po_ambiguity unambiguous, r_po_found found, r_po_status open, r_required_fields complete, r_tolerance_pct within_balance, r_vendor_po_mismatch match, r_vendor_status approved, engine_floor not applied, engine_floor_reference not applied.
No-PO trail: the same, except r_currency_mismatch / r_po_status / r_tolerance_pct / r_vendor_po_mismatch are `info` (not evaluable) and the two triggers above.
**Clean invoices that got flagged along the way:** 24429 was flagged by `r_arithmetic` (4 x 461.48 = 1,845.92 vs printed 1,845.94: the unit price is rounded to the cent). Fixed by a per-unit rounding allowance (decision 1). Before the currency fix it would also have gone to review on the 0.70 currency confidence.

## Decisions I need from the user
1. **Unit-price rounding allowance** (SPEC 53): line math allows an extra 0.005 per unit (`arithmetic_unit_price_rounding`; 0 = strict). Recommendation: keep; without it a clean real invoice goes to review.
2. **Missing tax line now checked as "no tax printed"** (SPEC 52): a total above subtotal + adjustments flags instead of being skipped. Recommendation: keep (it verifies the adjustments on both real invoices).
3. **No-PO invoices get `request_info`** (existing rule severity 2), which is odd for a vendor that never prints a PO reference (SuperStore). Recommendation: keep for now; decide in M3 whether "no PO" should be review.
4. **`$` -> USD at 0.85** (SPEC 46): a non-USD "$" vendor is only caught if the PO currency differs. Recommendation: keep.

## Assumptions added to SPEC section 11
46-53 (this stage). Earlier: 8-20 (M0), 21-35 (M1), 36-37, 38-45 (M2).

## Known risks or gaps
- PO matching with no PO reference scored 0.55 and 0.57 against the 0.50 minimum (vendor + amount + lines only): thin margin, and it needs PO line descriptions that resemble the invoice's.
- Grounding compares against the text layer only; scanned pages get `unavailable` (no penalty) so a misread scan is protected by model confidence alone.
- 4096-token output cap on very long invoices is still untested live (the two real replies used about 920 tokens).
- Working-tree files may have CRLF locally; git normalises to LF.
