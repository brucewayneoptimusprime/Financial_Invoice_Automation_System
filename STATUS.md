# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. The answer key `data\manifest.md` is verified (5 entries, `"verified": true` set at your instruction, nothing else changed) and committed. **Stopped as instructed: M3 is not started.**
- The eval run below was a free replay of the recorded live responses; no API call was made this round.

## Test count and result
**1603 passed, 0 failed, 2 deselected** (`pytest -W error`). No code changed this round.

## What changed
- `data\manifest.md` committed with all 5 entries verified.
- Ran `python -m app.extraction.eval --replay data\recordings` (result below).

## Eval result (replay of the 5 recorded live responses, 5 of 5 files scored)
- **Every scored field correct: 67 of 67 (100.0%); 5 of 5 files fully correct.** No misses, no hallucinations, no wrong values.
- Per field (5 files each): vendor_name, vendor_tax_id, vendor_address, document_type, invoice_number, invoice_date, currency, po_reference, subtotal, tax, total, line_items all 5/5; adjustments 7/7.
- Confidence: correct answers average 0.88 effective (0.91 model raw), n=47; no wrong answers, so calibration cannot be assessed.
- Grounding: 27 exact, 20 value_present, none not_found or mismatched. All 5 used `text_and_vision`.
- Cost of the recorded run: 34,040 tokens in, 4,668 out, $0.114760 (already spent; the replay costs nothing).

## Decisions I need from the user
1. Review the eval output above, then say when to start M3. Recommendation: before M3, extend the answer key beyond this one SuperStore layout (a scan, a tax line, a stated PO number, a non-USD invoice), because 100% on five near-identical documents is weak evidence.

## Assumptions added to SPEC section 11
None this round (46-58 earlier).

## Known risks or gaps
- The 100% is narrow: five near-identical SuperStore layouts, one line item each, no tax line, no PO reference, USD. Half of the "correct" fields are correct nulls (tax, tax id, address, PO reference). It does not test scans, multi-page documents, tax, other currencies or PO references.
- The drafts were generated from the same extraction being scored, so the score depends entirely on your check of the PDFs.
- Grounding uses the text layer only; scanned pages are `unavailable` (no penalty).
- The 4096-token output cap is untested on long invoices (these used about 930 output tokens each).
- The two existing `@pytest.mark.live` tests were not run.
