# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. All 6 samples re-recorded live with prompt `extract-v4` (replay works again). **M3 is planned in PLAN.md and waiting for your decisions; no M3 code exists.**
- The IQ entry in `data\manifest.md` is a draft (`"verified": false`); I did not touch `verified` and did not edit any values.

## Test count and result
**1640 passed, 0 failed, 2 deselected** (`pytest -W error`). +2 (tests on the live v4 IQ reply).

## What changed
- Live re-record of the 6 samples (5 SuperStore + IQ) with extract-v4: **tokens in 40,583 / out 5,929, cost $0.140456**, 55.9 s API time, all extracted, none degraded. A draft entry for the IQ scan was appended to `data\manifest.md`; the five verified entries were not touched.
- The live v4 IQ reply is saved as a fixture (`iq_electronics.v4.reply.json`) with tests; PLAN.md marks M3 decision 1 done and records your seed rule (PO descriptions must resemble real invoice line text).

## Re-verification
- **5 SuperStore entries: unaffected.** Scored against the new v4 responses: 67 of 67 fields correct (100%), 5 of 5 files fully correct, `tax` null in all five (expected null), mean confidence of correct answers 0.88 effective / 0.92 raw (was 0.91 raw). Grounding across all files: 26 exact, 21 value_present, 8 unavailable (the 8 are the scan); versus 27/20 before on the five PDFs, one item moved from exact to value_present (snippet wording varies between calls), no accuracy effect.
- **IQ, tax: fixed on a live model.** `tax` is null, `included_in_total` true, the CGST/SGST 9.00 rates are in `extraction_notes`. Arithmetic passes.
- **IQ, currency: NOT returned this time (regression).** The v3 call returned INR from "Rupees Four Thousand Nine Hundred only"; the v4 call returned no currency at all (`found=false`). Currency is a required field, so the rules give `request_info` (asking the vendor for a currency that is printed on the page). The prompt does not tell the model that a currency named in words counts; this looks like call-to-call variation, not the tax edit, but I cannot prove that from one call.
- **IQ, subtotal: now null** (v3 returned 4,900 from the "Total: 1 ... 4,900.00" row). Defensible: no subtotal is printed. Arithmetic then runs the line-math check only.
- **IQ draft in the manifest, checked against the image:** vendor_tax_id, invoice number, date 2025-01-05, total 4900.00, address, the one line and no adjustments match what is printed. **Do not verify it as drafted:** `currency: null` should be `"INR"` (the words on the page). The GSTIN/PAN sit under the "Details of Consignee" header in the layout and the model flagged that ambiguity; they match the vendor's Hyderabad registration (state code 36, PAN AAFCE1683D embedded in the GSTIN), but confirm on your side.

## Decisions I need from the user
1. **Currency in words (new):** add one prompt line (v5): a currency named only in words ("Rupees", "US Dollars") is returned as its ISO code when unambiguous. Cost: every prompt change changes all recording keys, so replay needs re-recording again (about $0.14, or $0.024 if only the IQ file is re-run and the five PDFs are re-recorded later). Alternative: leave it and accept `request_info` for words-only currency. Recommendation: fix it, and let me change replay keys to ignore prompt text is NOT proposed.
2. **M3 decisions:** the 8 decisions are pasted in full in my reply (decision 1 is done; 2-8 are open).

## Assumptions added to SPEC section 11
None this round (46-60 earlier).

## Known risks or gaps
- **M3 seed data must use realistic PO line descriptions** (your rule): the IQ line "APPLE IP 16 PRO MAX SL CS MGS PLM MYYW3Z Del.: 1801" scored 0 overlap against a shortened PO line and a correct match fell under the 0.50 minimum.
- Extraction is nondeterministic between calls (currency and subtotal on the IQ scan changed); one verified run per file is not a stability measurement.
- Scans have no grounding beyond the snippet checks; model confidence carries them.
- The IQ scan has no verified manifest entry yet.
