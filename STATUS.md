# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete and verified. **M3 (Pipeline) is planned in PLAN.md ("M3 plan") and waiting for your approval; no M3 code exists.**
- The live IQ Electronics scan run has NOT been made: it needs your go-ahead and the file path (the file is not in `data\invoices`).

## Test count and result
**1603 passed, 0 failed, 2 deselected** (`pytest -W error`). No code changed this round.

## What changed
- PLAN.md: added the M3 plan (module layout, exact writes per table, explainer/drafter constraints, seed coexistence, offline test list, build stages, 8 decisions).
- Offline check for the plan (throwaway script, nothing committed): the 5 real invoices each match their own PO confidently against a 5-PO SuperStore seed (top scores 0.57-0.60, next 0.42-0.46), so the seed design works, with a thin margin over the 0.50 minimum.

## Decisions I need from the user
1. Give me the IQ Electronics file path and say go for the one live run (`python -m app.extraction.cli "<path>" --record data\recordings`, about $0.02-0.03).
2. Approve or amend the 8 decisions at the end of the M3 plan (each has a recommendation).

## Assumptions added to SPEC section 11
None this round (46-58 earlier).

## Known risks or gaps
- None of the 5 SuperStore invoices prints a PO number, so against the demo seed each will go to `review` (matched_without_reference); the approve path needs a labelled controlled variant.
- The real explainer/drafter prompts will have no live evidence until you approve a live run; offline they are tested through doubles, checkers and deterministic templates.
- The M2 extraction CLI still calls the API whenever `--replay` is absent (plan decision 8 proposes a `--live` guard).
- Extraction accuracy is verified only on five near-identical SuperStore layouts.
