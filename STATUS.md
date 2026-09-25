# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete; two approved fixes from the IQ Electronics scan are in and committed. **M3 is planned in PLAN.md ("M3 plan") and still waiting for your decisions; no M3 code exists.**
- Live spend this round: none (the earlier IQ extraction cost $0.023214).

## Test count and result
**1638 passed, 0 failed, 2 deselected** (`pytest -W error`). +35 this round.

## What changed
- **Prompt `extract-v4`** (SPEC 59): a tax printed only as a rate is `not found`, never computed; the rates go in `extraction_notes`; the `included_in_total` flag carries the model's best read even when tax is null (the wire converter now keeps that one flag when `found=false`).
- **Grounding** (SPEC 60): a config table `currency_name_map` (rupees, dollars, euros, pounds, ...) is consulted before `value_mismatch`; longest name wins, so "Australian Dollars" does not vouch for USD.
- **Fixtures:** the recorded IQ reply and the image are in `backend/tests/fixtures/real/`; `data\invoices\image_based_invoice.jpg` committed as a sample.

## Re-run of the recorded IQ response (offline, no new call)
A plain `--replay` of the recording now misses: the prompt is part of the request key, so **all 6 recordings made with extract-v3 no longer replay** (the eval's `--replay data\recordings` would miss for every file). I ran the recorded reply through the new post-processing with a scripted client instead:
- **Currency:** INR, model 0.85, effective 0.85, grounding `unavailable` (a scan has no text layer) - **no longer `value_mismatch`** (it was 0.3).
- **Tax: still 882.00** (grounding `value_mismatch`, capped 0.3, `included_in_total` null). Expected: that reply came from the old prompt, and the fix is in the prompt, so it cannot show here. What is proven offline: the new prompt says it; a reply that follows it (tax not found, flag yes, rates in the notes) gives `tax` null, `included_in_total` true, and `r_arithmetic` passes; the old reply is still caught (arithmetic flags it).
- Rest unchanged: GSTIN 36AAFCE1683D1ZT (0.9), date 2025-01-05 capped at 0.5 (ambiguous, review), total 4900.00. Through the rules with a hand-written IQ vendor (matched by GSTIN) and PO: decision `review`, the only low-confidence required field is `invoice_date`.

## Decisions I need from the user
1. **Confirm the prompt fix on a live model?** Re-record the 6 samples with extract-v4 (about $0.14: 5 SuperStore + IQ, `python -m app.extraction.eval --live --record data\recordings` after adding the IQ file to the manifest, or the CLI per file). This also restores replay for the eval. Recommendation: yes, one run, then re-verify the IQ draft in `data\manifest.md`. Alternative: keep the old recordings by making replay ignore the prompt (weakens fidelity; not recommended).
2. **M3 decisions:** unchanged; see the 8 decisions at the end of the "M3 plan" in PLAN.md.

## Assumptions added to SPEC section 11
59, 60 (earlier: 46-58).

## Known risks or gaps
- Whether the live model now returns tax null on the IQ scan is unproven until decision 1.
- PO matching needs PO line descriptions that resemble the invoice's: the IQ line ("APPLE IP 16 PRO MAX SL CS MGS PLM MYYW3Z Del.: 1801") scored 0 overlap against a shortened PO description. The M3 demo seed must use realistic descriptions.
- Scans have no grounding beyond checks against the snippet (9-10 of 11 items `unavailable`); model confidence carries them.
- Extraction accuracy is verified on five near-identical SuperStore layouts; the IQ scan has no verified manifest entry yet.
