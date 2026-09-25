# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete; end-of-M3 re-record with extract-v5 done ($0.143; IQ currency now INR).
- **M4 (API + live run view): plan approved (all 7 decisions as recommended). Stage 1 of 5 done:** runner changes.
- Next: stage 2 (API: app factory, `serve` with the live rule, upload + worker, run view, runs list, page images).
- No live calls in M4 (owner runs `--live` after seeing the UI on replay).

## Test count and result
**1854 passed, 0 failed, 2 deselected** (`pytest -W error`); stage 1 added 9.

## What changed (M4 stage 1)
- `app/pipeline/summary.py`: `stage_started` / `stage_completed` events and one whitelisted summary per stage (SPEC section 11 item 69).
- `run_pipeline(..., run_id=None, source_name=None)`: the API can choose the run id and record the uploaded file's real name.
- 14 more audit events per run (about 58 in total for a SuperStore invoice). The one M3 test that listed stage order now skips the timing events; everything else is unchanged.
- New tests: pairs in pipeline order with durations; placement around each stage's own events; `stage_started` visible from a second connection before the stage runs; summary content (review and the synthetic approve variant); no page/invoice/model text in summaries; chosen and invalid run ids; failed act stage.

## Decisions I need from the user
None right now.

## Assumptions added to SPEC section 11
69 (stage timing events). Earlier: 61-68 (M3).

## Known risks or gaps
- The explain card completes a moment before the explanation text itself appears (it is written in the act transaction, as in M3).
- The real explainer/drafter have never run live.
- Extraction varies between calls (IQ vendor name wording on the v5 pass: capped at 0.30).
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
