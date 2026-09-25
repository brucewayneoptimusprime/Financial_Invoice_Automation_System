# STATUS

Last updated: 2026-09-26. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete. **M4 stages 1-4 committed; M4 stage 5 (your manual browser check on replay) is still open** and gets its own commit when you confirm it.
- **PO integration** (feature work before M5/M6, at your priority; plan approved with all 9 decisions as recommended): **stages 1-5 of 7 done** (the "Why" fix; PO backend; PO screens + manual form; PO drafter; text and document tabs).
- Next: PO stage 6 (multi-invoice upload, and upload from the PO detail page).
- No live calls. The first real PO drafts (text/document) are your live check after stage 7.

## Test count and result
- Backend: **2009 passed, 0 failed, 2 deselected** (`pytest -W error`); PO stage 1 added 5, stage 2 added 43, stage 4 added 50.
- Frontend: **50 passed** (vitest; stage 1 added 8, stage 3 added 11, stage 5 added 6); `tsc --noEmit` clean; `vite build` OK.

## What changed (PO stage 5: text and document tabs)
- New PO screen: tabs Form | Describe in text | Upload a document. Text and document produce a draft (spinner while the model works), then the SAME `POForm` pre-filled: model-filled fields show "from the model" + confidence + evidence status + the source quote (click opens the rendered page for PDF/image drafts); fields the source did not state are empty and marked "not in the source"; warnings (no currency stated, several POs, text addressed to an AI, ambiguous vendor) and the draft's notes above the form; "Nothing has been saved" stated; the vendor suggestion pre-selected or a new vendor proposed (created only on Save). Save sends the form values plus the draft id.
- A failed draft says why and offers "Try again" / "Use the empty form". Offline mode shows "use the Form tab" and makes no call. Switching tabs or leaving with a draft asks first.
- Fixtures `po_draft_*.json` from the real endpoints with scripted model replies.

## What changed (PO stage 4: the PO drafter; model calls only through fakes)
- `app/po/`: `wire.py` (union-free PO schema), `prompts.py` (`po-draft-v1`, fingerprint pinned in a test), `drafter.py` (call, one repair retry, post-processing, grounding, reader-instruction scan; a failure is a `failed` draft, never an exception), `readers.py` (PDF/PNG/JPG via the invoice ingest; DOCX, XLSX, CSV text; refusals), `service.py` (draft -> form pre-fill, per-field marks, vendor suggestion or proposed new vendor, the form's own issues, the draft file).
- `grounding.ground_item`: a public one-item wrapper over the existing invoice grounding (no behaviour change for invoices).
- Endpoints: `POST /api/pos/drafts/text`, `POST /api/pos/drafts/document`, `GET /api/pos/drafts/{id}/pages/{n}`. Offline: 503. They write nothing to the database (tested across every table); only Save does.
- SPEC section 11 item 73. New dependency `openpyxl` (in `pyproject.toml`).

## What changed (PO stage 3: PO screens, manual form)
- Top navigation: Invoices | Purchase orders. Routes `/pos`, `/pos/new`, `/pos/:id`.
- PO list: search (PO number or vendor), status filter, total and derived balance, status, matched-invoice count, how it was entered.
- PO detail: total / committed / balance / awaiting review; matched invoices (link to each run, decision at run time, status now; historic ones marked); lines; ledger; "also considered in (not matched)"; where the PO came from (source, model, fields the person changed, typed text). Refreshes every 5 s so newly uploaded invoices appear.
- `components/POForm.tsx`: the ONE form all three paths will use. Validates as you type (debounced `POST /api/pos/validate`, nothing saved), shows errors in red and warnings in amber per field, keeps Save disabled while an error remains, offers "use the sum of the lines" only as a button, creates a new vendor inline (stated: status new). Save sends exactly the form's values. Leaving with unsaved changes asks first. Draft markers ("from the model", confidence, evidence, source quote, "not in the source") are built in for stage 5.
- Fixtures `po_*.json` / `vendors.json` generated from the real backend views (offline).

## What changed (PO stage 2: PO backend, no model)
- `app/po/`: `models.py` (what the form posts), `validate.py` (deterministic blocking errors and warnings, shared by all three entry paths), `store.py` (the ONLY writer of POs: new vendor + PO + lines in one transaction), `drafts.py` (draft files and `edited_fields`), `views.py` (PO list / detail read models).
- Endpoints (`app/api/routes_po.py`): `GET /api/vendors`, `GET /api/pos?q=&status=`, `GET /api/pos/{id}`, `POST /api/pos/validate` (saves nothing), `POST /api/pos` (Save: 201; 422 with per-field issues; 409 for a PO number already used).
- PO detail: PO + vendor + lines; total / committed / balance (derived from the ledger) / awaiting review (matched invoices still in review, which do not consume); matched invoices (`invoices.po_id`, with run status, decision, effective status; seeded historic invoices marked); ledger entries; "also considered in" (runs where the PO was ranked but not matched, from the stored candidate events); provenance.
- Config: `po_*` settings (prompt version, limits, drafts dir), `unsupported_currencies`, `ui_max_files_per_upload`. `data/po_drafts/` is gitignored. SPEC section 11 item 72.
- Matching unchanged: a PO entered through the form, with the same facts as seeded PO-SS-001, gets exactly the same candidate score from the existing engine.

## What changed (PO stage 1: the "Why" fix, UI only)
- `frontend/src/reasons.ts`: each "Why" bullet shows only the plain sentence (the engine's message, with field ids shown as the fields-table labels: `invoice_date` -> "invoice date"). Rule id, rule name, outcome code, severity and cited fact ids are behind a per-bullet "Technical details" button. A model-written reason stays as written (its facts go in the details); an unknown form is shown as is, never hidden.
- The same treatment for the review-queue reason in "What was written" (decision 8).
- `backend/tests/pipeline/test_reason_format.py`: pins the two template reason forms and the review-reason form the UI parses (test only; no backend code changed).

## M4 in one paragraph (details in PLAN.md and SPEC items 69-71)
FastAPI app (`python -m app.api.serve --replay DIR | --live | --offline`; refuses without a mode), upload -> one-at-a-time worker, run view from SQLite, page images, SSE stream tailing `audit_events`; React UI with the live 7-stage timeline and the result view. To run it for your stage-5 check: window 1 `cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo`; window 2 `cd C:\Zamp_ai_Automation\frontend; npm run dev`; open http://localhost:5173.

## Decisions I need from the user
- Confirm M4 stage 5 when you have checked it.

## Assumptions added to SPEC section 11
72-73 (PO entry, PO drafting). Earlier: 69-71 (M4), 61-68 (M3).

## Known risks or gaps
- PO drafting (text and document) has run only against scripted replies; the prompt has no live evidence until your live check. A replay server has no PO recordings, so on replay every draft comes back failed (`replay_miss`); use `--live` for real drafts.
- The "Why" parser depends on the template wording; the contract test fails first if it changes.
- The real explainer/drafter have never run live. Extraction varies between calls.
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
- Node here is 22.19; jsdom is pinned to 29 (jsdom 30 needs 22.22+). Without a declared Content-Length an upload is buffered before the size check.
