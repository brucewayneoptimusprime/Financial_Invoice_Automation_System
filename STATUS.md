# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete. **The end-of-M3 re-record is done** (owner-approved): all 6 samples re-recorded live with extract-v5, so `--replay data\recordings` works again for the eval, the extraction CLI and the pipeline CLI. No code, prompt, seed or manifest changes this session.
- **M4 (API + live run view): plan written in `PLAN.md` (bottom section), awaiting approval. No M4 code exists.**
- The explainer/drafter have still never run live (owner: check them later in the M4 UI with real invoices).

## Test count and result
**1845 passed, 0 failed, 2 deselected** (`pytest -W error`), unchanged; run after the re-record.

## The re-record (live, extraction only)
`python -m app.extraction.eval --live --record ..\data\recordings --max-cost 0.20`: 6 calls, **$0.143174** (in 41,747 / out 5,968 tokens), 53 s of API time. Eval on the 5 verified SuperStore entries: **67/67 fields correct** (same as v4). Grounding: 28 exact, 19 value_present, 8 unavailable, 1 value_mismatch. The old v3/v4 recordings are still in the (gitignored) folder; they are keyed by prompt fingerprint and simply never hit.

**IQ manifest entry re-checked:** `currency` is now **`"INR"`** (confidence 0.90, source text "Rupees Four Thousand Nine Hundred only"). So extract-v5 fixed the finding. Everything else matches the draft entry except one new difference:
- `vendor_name` came back as `"IQ (Electronics Mart India Ltd.)"` (the draft says `"IQ (A unit of Electronics Mart India Ltd.)"`); the model dropped "A unit of", so grounding caps it at **0.30** (`value_mismatch` against its own snippet). This happens from call to call; it is not a regression the prompt caused.
- I did **not** edit `data/manifest.md`: the IQ entry is still a draft (`verified: false`) with `currency: null`. When you verify it, set `currency` to `"INR"` and pick the vendor-name form you want as the answer.

## The six real invoices, end to end (replay of the new recordings; explainer/drafter = templates on replay miss)
Scratch database reset to the demo dataset, run in this order:
| Invoice | Decision | Why (one line) |
|---|---|---|
| SuperStore 10963, 24429, 14021, 6459, 14130 | **review** (each) | a confident match on its own PO-SS-00x, but the invoice prints no PO number (`r_po_found` matched_without_reference); everything else passes |
| IQ Electronics scan | **review** (was request_info) | currency INR is now present, so all 5 required fields are there and PO-IQ-2025-001 matches (0.587: vendor by GSTIN, amount 4,900 of 5,000, line overlap 0.93); still review because no PO number is printed, vendor_name confidence is 0.30 (the grounding cap) and the invoice date only 0.50 (05-01-2025 is ambiguous) |

So none of the six requests info any more; no vendor email draft comes out of the real set. The request_info/reject paths are still covered by the offline variants.

## Decisions I need from the user
1. **Approve (or change) the M4 plan** in `PLAN.md` (section "M4 plan"), including its decision list.
2. When convenient: verify the IQ manifest entry (see above).

## Assumptions added to SPEC section 11
None this session. Latest: 61-68 (M3).

## Known risks or gaps
- The real explainer/drafter have never run; the claim checks are strict, so expect some template fallbacks at first.
- Extraction varies between calls (IQ: the currency across v3/v4/v5, the vendor-name wording now); one pass per file does not measure stability.
- `to` is always NULL (no vendor contact data); a blocked-vendor reject writes an internal notification instead of an email.
- With no printed PO number, none of the six real invoices can auto-approve (owner's rule); the approve path is shown only by the labelled synthetic controlled variant.
