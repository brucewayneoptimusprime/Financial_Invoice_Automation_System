# STATUS

Last updated: 2026-09-25. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M2 complete. **M3 (Pipeline): Stages 1-2 of 4 done** (seed + persistence; digest, template explanation/drafts, actions, runner). Stage 3 (model explainer/drafter with claim checks) and Stage 4 (CLI + six-invoice end-to-end) follow; I stop after Stage 4.
- No live call, no re-record.

## Test count and result
**1723 passed, 0 failed, 2 deselected** (`pytest -W error`). +48 this stage. 12 mutation checks (no ledger commit, stale-snapshot check removed, blocked vendor emailed, invoice saved only on approve, drafts marked sent, no review item, draft for review, PO status not updated, act stage not atomic, vendor status made vendor-facing, non-monotonic audit seq, overwritable final_decision): all killed.

## What changed (Stage 2)
- `app/pipeline/runner.py`: `run_pipeline(path, conn, ...)` runs ingest -> extract -> match -> validate -> decide -> explain -> act using the M1/M2 stages unchanged; audit events are persisted after every stage; the act stage is ONE `BEGIN IMMEDIATE` transaction (invoice, lines, ledger/review/draft, explanation event, run completion); an approve is re-verified there and escalated to `review` if the records changed; any error rolls back and marks the run `failed`.
- `digest.py` (numbered facts from the trail only), `explain.py` / `draft.py` / `templates.py` (deterministic explanation and drafts, also the Stage 3 fallback), `actions.py` (decision table), persistence for ledger, PO status, review queue, drafts.
- Behaviour, per decision (tested on the demo DB): approve = ledger commit + PO status + ready for payment; review = review_queue row, no email; request_info / reject = draft (status `draft`, never sent, `to` NULL), a blocked-vendor reject = internal `notification`, only vendor-facing rules ever reach a vendor email.
- **Synthetic labelling (your requirement):** the only approve path uses a controlled variant (real invoice 24429 with `PO-SS-002` edited into the recorded reply). It is named `controlled_variant_synthetic_*` in tests, tagged `[SYNTHETIC CONTROLLED VARIANT ...]` inside the edited reply's notes, and labelled as synthetic in the helper (`LABEL_CONTROLLED`). It is never used as a real sample.
- Config: `vendor_facing_rules`, `no_vendor_email_outcomes`, `review_reason_max_chars`. SPEC section 11 items 64-65.

## Decisions I need from the user
None for Stage 2. Prompt/wire changes so far that need the end-of-M3 re-record: **extract-v5 only** (currency in words). Re-record cost if nothing else lands: 6 calls, about $0.14; I will state the final number before running it.

## Assumptions added to SPEC section 11
64, 65 (earlier: 46-63).

## Known risks or gaps
- Explanations and drafts are template-only until Stage 3; their real-model quality is unmeasured (needs an approved live run).
- Recordings are stale for replay until the end-of-M3 re-record.
- The IQ scan still has no verified manifest entry; with the current v4 reply its currency is missing, so it would be a request_info until the v5 re-record (Stage 4 pins that).
