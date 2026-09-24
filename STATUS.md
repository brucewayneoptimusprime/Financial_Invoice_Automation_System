# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete: Stage 4 (grounding, injection scan, engine touch points, real-invoice end-to-end) and **Stage 5 (eval script, manifest parser, draft manifest)** are done and committed. **Stopped as instructed; M3 is not started.**
- No live API call was made in either stage. The eval was run once offline with `--replay` on the two real recordings.

## Test count and result
**1583 passed, 0 failed, 2 deselected** (`pytest -W error`, ~51 s). Stage 4 added 210 (1500 total), Stage 5 added 83. Mutation checks: 23 (Stage 4) + 16 (Stage 5, live guards, verified-only scoring, draft rules, scoring verdicts, ceiling and schema stops): all killed except one equivalent mutant (unreachable code).

## What changed
- **Stage 4:** grounding (`value_present` at 0.85 on the real label-separated PDFs, never raises confidence), `$` currency at a configured 0.85, injection scan, three new floor reasons, system-side failure -> `review` / vendor-side -> `request_info`, adjustments in `r_arithmetic`, tax-id vendor resolution, `r_document_type`. SPEC section 11 items 46-53.
- **Stage 5:** `python -m app.extraction.eval` (`--dir --manifest --mode --replay --live --record --max-cost --draft-manifest`), `manifest.py` (parser + draft writer), `scoring.py`. SPEC items 54-57.
- **`--live` guard:** without `--live` and without `--replay` the eval refuses (exit 5) before any client exists; the real client is built in one function behind an `allow_live` check, enforced by a structural test and by tests that fail if the client is ever constructed without the flag. `--record` also needs `--live`.
- **Manifest:** only `"verified": true` entries are scored; drafts are always `false`, existing entries are never edited, the writer refuses to write `true`.
- **Real invoices end to end (unchanged from Stage 4):** matching PO -> approve (all 13 rules and both floors pass) for both 10963 and 24429; no PO -> request_info (`r_po_found` no_reference + floor). No clean invoice is flagged now; 24429 was, until the line-math rounding allowance (SPEC 53).

## Decisions I need from the user
1. **Unit-price rounding allowance** (SPEC 53, 0.005 per unit): keep? Recommendation: keep; without it a clean real invoice (24429) goes to review.
2. **Missing tax line = "no tax printed"** (SPEC 52): keep? Recommendation: keep; it is what verifies the shipping/discount adjustments on both real invoices.
3. **No-PO invoices get `request_info`** (existing severity 2 on `r_po_found`); odd for a vendor that never prints a PO number. Recommendation: keep until M3, then decide whether no-PO should be `review`.
4. **`$` -> USD at 0.85** (SPEC 46): a non-USD "$" vendor is only caught if the PO currency differs. Recommendation: keep.
5. **To start the answer key:** run `python -m app.extraction.eval --live --record data\recordings --draft-manifest` (about $0.12 for the 5 sample invoices; needs your go-ahead), check each draft in `data\manifest.md` by hand, flip `verified` to `true`, then re-run with `--replay data\recordings` for free. I have not run it.

## Assumptions added to SPEC section 11
46-53 (Stage 4), 54-57 (Stage 5). Earlier: 8-20 (M0), 21-35 (M1), 36-37, 38-45 (M2).

## Known risks or gaps
- `data\manifest.md` does not exist yet, so nothing is scored on the real invoices; accuracy is unmeasured until you verify some drafts. Three of the five sample invoices have no recording (replay misses until they are recorded live).
- PO matching without a PO reference scored 0.55 and 0.57 against the 0.50 minimum (vendor + amount + lines only): thin margin.
- Grounding uses the text layer only; scanned pages are `unavailable` (no penalty).
- The 4096-token output cap on very long invoices is untested live (the real replies used about 920).
- The two existing `@pytest.mark.live` tests were not run. Working-tree files may have CRLF locally; git normalises to LF.
