# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete, plus the follow-ups you approved (below). **Stopped as instructed: waiting for you to verify `data\manifest.md`; M3 is not started.**
- The approved live draft run was made once (5 paid calls, all succeeded). No other live call was made.

## Test count and result
**1603 passed, 0 failed, 2 deselected** (`pytest -W error`). +20 this round.

## What changed
- **Genuine arithmetic errors still flag** despite the unit-price allowance: a wrong quantity, a wrong unit price, transposed digits, and errors of 4 cents or more on 4 units all fail `r_arithmetic`; the allowance boundary (3 cents for 4 units) is pinned; the same mistakes injected into the real 24429 reply are caught end to end.
- **`r_po_found` split** (SPEC 58), tested at rule and pipeline level:
  - stated reference that matches no PO -> severity 2 -> `request_info`
  - no reference, confident unambiguous vendor+amount+lines match -> severity 1 -> `review`
  - no reference, no confident match -> severity 2 -> `request_info` (as before)
- **Draft manifest run** (live, `--record data\recordings --draft-manifest`): 5 drafts written to `data\manifest.md`, all `"verified": false`; I did not touch `verified`. `data\manifest.md` is uncommitted so your edits show as a clean diff.

## Real invoices end to end (SuperStore vendor, PO-SS-1 with plenty of balance; identical for 10963 and 24429)
| Case | Decision | Triggered |
|---|---|---|
| Matching PO, invoice prints no PO reference | **review** (was approve) | `r_po_found` matched_without_reference (sev 1); the other 12 rules and both floors pass |
| No PO at all | **request_info** | `r_po_found` no_reference (sev 2), `engine_floor` no_po_candidates (sev 1) |
| Stated reference matching no PO | request_info | `r_po_found` reference_not_found (sev 2) (pipeline test, not a real invoice: neither sample prints a PO number) |

Consequence: the two SuperStore samples now go to review even with a perfect match, because they print no PO number. That is what you specified.

## Draft run totals (live)
5 invoices, all `text_and_vision`, all extracted, none degraded. **Tokens in 34,040 / out 4,668. Cost $0.114760** ($0.0224-$0.0235 each, 44.0 s API time). Grounding: 27 exact, 20 value_present, 0 not_found or mismatched. Drafts, at a glance (all SuperStore, USD, no tax line, no PO reference, one line item):

| File | Date | Subtotal | Adjustments | Total | Arithmetic |
|---|---|---|---|---|---|
| 14021 | 2013-03-07 | 9466.50 | shipping 205.01 | 9671.51 | adds up |
| 6459 | 2013-03-06 | 9515.00 | shipping 243.79 | 9758.79 | adds up |
| 14130 | 2013-03-07 | 7556.98 | discount -755.70, shipping 89.18 | 6890.46 | adds up |
| 24429 | 2013-03-07 | 1845.94 | discount -184.59, shipping 109.26 | 1770.61 | adds up |
| 10963 | 2013-03-07 | 5141.76 | shipping 196.32 | 5338.08 | adds up |

Arithmetic adding up shows the drafts are self-consistent, not that they match the documents: check each value against the PDF.

## Decisions I need from the user
1. **Verify `data\manifest.md`**: check each draft against its PDF, fix anything wrong, set `"verified": true` per entry. Then `python -m app.extraction.eval --replay data\recordings` scores them for free.
2. **Note on item 3:** you described the stated-but-unmatched reference as "as it is now (severity 2)", but it was severity 1 (the reference floor gave the review). I set it to 2 to match your stated outcome (SPEC 58). Recommendation: keep; say so if you wanted the old severity 1.

## Assumptions added to SPEC section 11
58 (this round); 53 marked owner-approved. Earlier: 46-57 (Stages 4-5), 8-45.

## Known risks or gaps
- Nothing is scored until you verify entries, so extraction accuracy is still unmeasured; all 5 samples are near-identical SuperStore layouts, so they say little about other formats (scans, tax lines, PO references, non-USD).
- Grounding uses the text layer only; scanned pages are `unavailable` (no penalty).
- The 4096-token output cap is untested on long invoices (these used about 930 output tokens each).
- The two existing `@pytest.mark.live` tests were not run.
