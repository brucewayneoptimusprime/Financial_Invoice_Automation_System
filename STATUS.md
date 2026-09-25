# STATUS

Last updated: 2026-09-26. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete. **M4 stages 1-4 committed; M4 stage 5 (your manual browser check on replay) is still open** and gets its own commit when you confirm it.
- **PO integration** (feature work before M5/M6, at your priority; plan approved with all 9 decisions as recommended): **stage 1 of 7 done** (the "Why" fix).
- Next: PO stage 2 (PO backend without a model: validate, save, vendor/PO list/detail endpoints).
- No live calls. The first real PO drafts (text/document) are your live check after stage 7.

## Test count and result
- Backend: **1916 passed, 0 failed, 2 deselected** (`pytest -W error`); PO stage 1 added 5 (the template-format contract test).
- Frontend: **33 passed** (vitest; stage 1 added 8); `tsc --noEmit` clean.

## What changed (PO stage 1: the "Why" fix, UI only)
- `frontend/src/reasons.ts`: each "Why" bullet shows only the plain sentence (the engine's message, with field ids shown as the fields-table labels: `invoice_date` -> "invoice date"). Rule id, rule name, outcome code, severity and cited fact ids are behind a per-bullet "Technical details" button. A model-written reason stays as written (its facts go in the details); an unknown form is shown as is, never hidden.
- The same treatment for the review-queue reason in "What was written" (decision 8).
- `backend/tests/pipeline/test_reason_format.py`: pins the two template reason forms and the review-reason form the UI parses (test only; no backend code changed).

## M4 in one paragraph (details in PLAN.md and SPEC items 69-71)
FastAPI app (`python -m app.api.serve --replay DIR | --live | --offline`; refuses without a mode), upload -> one-at-a-time worker, run view from SQLite, page images, SSE stream tailing `audit_events`; React UI with the live 7-stage timeline and the result view. To run it for your stage-5 check: window 1 `cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo`; window 2 `cd C:\Zamp_ai_Automation\frontend; npm run dev`; open http://localhost:5173.

## Decisions I need from the user
- Confirm M4 stage 5 when you have checked it.

## Assumptions added to SPEC section 11
69-71 (M4). Earlier: 61-68 (M3).

## Known risks or gaps
- The "Why" parser depends on the template wording; the contract test fails first if it changes.
- The real explainer/drafter have never run live. Extraction varies between calls.
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
- Node here is 22.19; jsdom is pinned to 29 (jsdom 30 needs 22.22+). Without a declared Content-Length an upload is buffered before the size check.
