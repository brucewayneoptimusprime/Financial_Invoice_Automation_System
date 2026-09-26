# STATUS

Last updated: 2026-09-26. Repo: `C:\Zamp_ai_Automation` (spec `SPEC.md`, plan `PLAN.md`).

## Current milestone and state
- M0-M3 complete. M4 stages 1-4, PO integration 1-6, line-item consumption 1-4 and review actions 1-3 committed. Open with you: the browser checks (M4 stage 5, PO stage 7, review actions stage 4), the review of the line-item build, and whether you want a demo loader for the line-choice path.
- **Landing dashboard** (plan approved, all 8 decisions as recommended): **stages 1-2 of 3 done, plus the clickable-summaries follow-up. Stopped at stage 3: your manual browser check** (see "How to check the dashboard" below).
- **Your `data\app.db` is still schema version 1**: run `python -m app.db.migrate` from `backend\` (backup first) or start the server with `--reset-demo`.
- No live calls.

## Test count and result
- Frontend: **79 passed** (vitest; the follow-up added 7, dashboard stage 2 added 7, review actions 11); `tsc --noEmit` clean; `vite build` OK.
- Backend: **2142 passed, 0 failed, 2 deselected** (`pytest -W error`); dashboard stage 1 added 11, the follow-up 2 (`tests/api/test_dashboard.py`); no existing test changed. Earlier: review actions added 47. Earlier: the line-item build added 72 tests (stage 1: 20, stage 2: 30, stage 3: 13 + 1 re-based, stage 4: 7 + 2 assertions). Existing tests changed only for new tables/events/rule counts, **except ONE decision on a hand-written fixture** (stage 3 below). 
- Frontend: **54 passed** (vitest; stage 1 added 8, stage 3 added 11, stage 5 added 6, stage 6 added 4); `tsc --noEmit` clean; `vite build` OK.

## How to check the dashboard (stage 3, yours)
Window 1: `cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo`. Window 2: `cd C:\Zamp_ai_Automation\frontend; npm run dev`. Open http://localhost:5173.
- `/` is now the **Dashboard** (nav: Dashboard | Invoices | Purchase orders | Review queue). With a fresh demo database it shows 0 invoices, "No invoices yet: upload one", and the seeded POs: 6 POs, a USD card (39,500.00 total, 1,500.00 consumed, all of it not assigned to a line) and an INR card (5,000.00).
- **Invoices** is now at `/invoices` (upload + recent runs, as before). Upload a few of the six; back on the dashboard: invoices processed, the decision chips and bar, the review queue count, the recent runs, the items waiting for review (each links to its item).
- Approve one review item, then look again: the system decisions stay the same, the "Now, after review" line moves (1 approved), the USD consumed figure rises.
- LLM spend shows $0 on replay (replayed calls are not charged); after a `--live` run it shows the invoice runs and any PO drafts.
- The PO page's "Upload invoices" now opens `/invoices?po=...`.
- **Clickable summaries:** a decision chip (or its bar segment) opens Invoices filtered to that decision ("Runs the system decided: ...", with "Show all runs"); **Review** opens the review queue; each "Waiting for review" row opens that item's approve screen; each currency card opens Purchase orders filtered to that currency ("Currency: USD · show all currencies").

## What changed (dashboard follow-up: clickable summaries)
- Dashboard: the decision chips link to `/invoices?decision=approve|request_info|reject` and `/review` for Review; the proportion-bar segments link the same way (`tabindex -1`, since the chips are the keyboard links and the bar stays `aria-hidden`); each per-currency PO card links to `/pos?currency=<code>`. "Waiting for review" rows already opened `/review/<id>`; a test now pins it.
- Invoices list: `?decision=` (the system decision, `runs.final_decision`) filters the runs list (up to 50) with a heading and a "Show all runs" link; an unknown value is ignored. Backend: `GET /api/runs?decision=` (validated; `views.recent_runs` gained the optional filter; queued runs are left out when filtering, they have no decision yet).
- Purchase orders list: `?currency=` (and `?status=`) set the initial filters; the currency shows as "Currency: USD · show all currencies". Backend: `GET /api/pos?currency=` (case-insensitive; `po_list` gained the optional filter).
- No new decision logic, no visual redesign: a link style for the chips and a small filter note.

## What changed (dashboard stage 2: the screen)
- Routes: `/` -> dashboard, `/invoices` -> the upload screen (it was `/`). Updated links: brand and 404 -> dashboard; "← New invoice" and "Upload an invoice" on the run page -> `/invoices`; the PO page's "Upload invoices" -> `/invoices?po=<id>`. Nav gains **Dashboard** (highlighted on `/`); Invoices is highlighted on `/invoices` and on run pages.
- `screens/Dashboard.tsx`: stat cards (invoices processed + failed/running, decisions with chips + a CSS-only proportion bar that is `aria-hidden` next to the written counts + the "now, after review" line, review queue count linking to `/review`, LLM spend with the split), a Purchase orders section (count + status chips, one card per currency: total value, consumed, balance, not assigned to a line), and two lists (recent runs with decision and "now ..." when a reviewer changed the outcome; waiting for review with the plain reason and ready / line choices / cannot approve). Refreshes every 10 s. Empty state points to `/invoices`.
- `Stat` moved from the PO page into `components/common.tsx` (same look, an optional sub-line); the only new CSS is the dashboard grid and the bar.
- Fixtures `dashboard_empty.json` / `dashboard_busy.json` recorded from the real endpoint (offline; the busy one includes the labelled synthetic approve and request_info variants and one reviewer approval).

## What changed (dashboard stage 1: the endpoint)
- `app/api/dashboard.py` + `GET /api/dashboard?recent=8&review=5` (SPEC section 11 item 79): runs (processed / failed / running, the system decision per decision), outcomes now (after human review), review (open count + oldest open items via the review service), spend (invoice runs + PO drafts), POs (count, by status, per currency: total value, consumed, balance, consumed without a line), recent runs. Read-only (tested: no table changes).
- `views.recent_runs` gained vendor, invoice number, invoice total and current invoice status (LEFT JOIN; new fields only, so `/api/runs` returns them too).
- Demo seed, empty history: 6 POs (5 open, 1 partially billed); INR 5,000.00 (nothing consumed); USD 39,500.00 total, 1,500.00 consumed (all of it without a line: the seeded history), 38,000.00 balance.

## How to check review actions (stage 4, yours)
Window 1 (backend): `cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo` (a fresh demo database at schema v2; or first `python -m app.db.migrate` to keep your own `data\app.db`). Window 2: `cd C:\Zamp_ai_Automation\frontend; npm run dev`. Open http://localhost:5173.
- Upload one or more of the six invoices from `data\invoices`; each goes to review. The nav shows **Review queue** with the open count.
- Open an item: "Why it was held for review" in plain sentences; the approve panel shows the commit, the PO balance before -> after, the automatic allocation (invoice line 1 -> PO line 1) and the remainder going to the PO total. **Confirm approval** is one click; the result links to the PO.
- On the PO page: the new "Consumed, not assigned to a line" figure, consumed/remaining columns per line, and "How the commits are allocated".
- **Reject** on another item: the panel states that nothing is written; afterwards the PO page is unchanged.
- **The line-choice path will not appear with the six real invoices**: every one of their lines matches its PO line confidently. It is covered by the tests and the recorded fixtures (ambiguous: three candidates, the best pre-selected, a non-fitting line disabled with its numbers; bundled: "no specific line" pre-selected). If you want to see it in the browser I can add a small, clearly labelled demo loader for the synthetic scenarios (not built: say if you want it).
- Smoke check done here on a replay server (scratch DB): 10963 uploaded -> review item -> approved through the API with the preview's token: 5,141.76 on PO-SS-001 line 1 (line fully used), 196.32 against the PO total, balance 6,000.00 -> 661.92.

## What changed (review actions stage 3: frontend)
- Nav **Review queue** (open count, refreshed every 5 s). `/review`: Open / Resolved tabs; one row per item (vendor, invoice, total, PO, plain reason, "ready" / "N line choices" / "cannot approve"); no checkboxes, no bulk action.
- `/review/:id`: the reasons; the approve panel (PO, commit, balance before -> after, warnings, automatic allocations, a card per line needing a choice with ranked candidates, "another line of this PO", "No specific line: deduct from the PO total"; the best guess pre-selected; options that cannot fit disabled with remaining + allowance; `src/allocation.ts` repeats the server's fit check in integer cents so Confirm stays disabled until every choice fits; one submit); a 409 replaces the preview and says why; a 422 shows the server's message on its line; blocked items say why and keep Reject; Reject states that nothing is written; after an action the result and "Next item".
- PO detail: the "Consumed, not assigned to a line" stat with a one-line explanation, consumed/remaining per line, and the allocation rows (`GET /api/pos/{id}` gained `allocations`).

## What changed (review actions stage 2: approve and reject)
- `app/review/actions.py` + `POST /api/review-queue/{id}/approve` and `/reject` (SPEC section 11 item 78): one transaction each; re-verification (blockers, `state_token`), the allocation plan with the reviewer's choices, ledger commit, consumption rows (invariant checked before commit), PO status, invoice status, item resolved, audit events on the run. Reject never writes a ledger entry or an allocation.
- **Your five required tests** (`tests/review/test_review_actions.py`), all passing:
  1. all lines confident -> zero input: real invoice 10963 approved with `allocations: []`: one commit 5,338.08; `auto` row on PO-SS-001 line 1 (5,141.76, qty 4) + remainder 196.32 against the PO total; PO partially_billed, balance 6,000.00 -> 661.92; invoice approved; item resolved; `final_decision` still review. Also the synthetic clean scenario (two automatic lines).
  2. mixed confident + ambiguous -> 422 `allocation_required` listing exactly the ambiguous line with its 3 candidates, NOTHING written; resubmitted with a choice -> 200, the chosen line `manual_reviewer`, the other `auto`.
  3. over the remaining amount -> 422 `allocation_invalid`: "Invoice line 1 (1000.00) does not fit PO line 2: 400.00 remaining, allowance 8.00." Nothing written. The boundary (exactly remaining + allowance) is accepted, one cent over is refused.
  4. unassigned -> the PO page's `consumed_without_line` 0.00 -> 1,105.00 (the line + the remainder), every line's remaining amount and quantity unchanged, balance 395.00.
  5. reject -> no `ledger_entries` or `po_consumption` row; invoice rejected; item resolved/rejected; events `human_rejected`, `review_resolved`.
- Also tested: stale token -> 409 with a fresh preview (balance 5,990.00 after an injected commit) and nothing written, then approvable with the new token; double approve / approve-after-reject / reject-after-approve -> 409; each blocker -> 409 with reject still possible; a fault after the ledger insert rolls everything back; every invalid-choice code; `confirm` required (400); unknown item 404; the audit trail continues the run's seq; resolved items leave the open list; no bulk endpoint exists (only the four review paths).

## What changed (review actions stage 1: allocation planner + GET endpoints)
- `app/pipeline/allocation.py`: the pure planner used by BOTH the preview and (stage 2) the approval. Confident lines that still fit -> automatic rows; other lines -> the reviewer's choice (any line of the PO, or "no specific line"), with the per-line fit check = `evaluate_tolerance` on the PO line's remaining amount minus what earlier lines of the same approval took, using `r_tolerance_pct`'s current params; the remainder (tax, shipping) -> one row against the PO total; a negative remainder scales the lines pro rata; lines without an amount are not allocated; a PO with no amount-bearing lines takes everything on its total without asking.
- `app/review/service.py`: loads an item (invoice, stored line matches, PO lines with remaining amounts now), the blockers, the `state_token` (item/invoice/PO status + the PO's ledger and consumption rows), the approve preview (PO balance before/after, commit, tolerance, automatic rows, lines needing input with ranked candidates / other lines / fits / allowance / a suggested choice, the remainder, warnings), the list.
- `GET /api/review-queue?status=open|resolved&limit=` (open oldest first; `can_approve`, `lines_needing_input`, `open_count`) and `GET /api/review-queue/{id}` (item, run, invoice, approve preview, and the run view's `line_matches`, reused as is).
- On real invoice 10963: automatic line 1 -> PO-SS-001 line 1 (5,141.76, qty 4) + remainder 196.32; balance 6,000.00 -> 661.92. On the SYNTHETIC ambiguous scenario (vendor made `new` so it is held for review): line 2 automatic, line 1 needs a choice among PO lines 1-3 (line 3 cannot take 600.00).

## The six real invoices, before and after this build
Replay of the extract-v5 recordings, each run against a fresh demo database; "before" = commit `ffc0c05` (the code just before line items, run from a temporary git worktree), "after" = this build. Explainer/drafter: templates on replay.

| Invoice | Decision before | Decision after | PO | Line match (after) | `r_po_line_price` | Triggered checks (identical before/after) |
|---|---|---|---|---|---|---|
| SuperStore 10963 | review | **review** | PO-SS-001 | line_level: line 1 -> PO line 1, score 1.00 | within_tolerance | r_po_found matched_without_reference |
| SuperStore 24429 | review | **review** | PO-SS-002 | line_level: 1 -> 1, 1.00 | within_tolerance | r_po_found matched_without_reference |
| SuperStore 14021 | review | **review** | PO-SS-003 | line_level: 1 -> 1, 1.00 | within_tolerance | r_po_found matched_without_reference |
| SuperStore 6459 | review | **review** | PO-SS-004 | line_level: 1 -> 1, 1.00 | within_tolerance | r_po_found matched_without_reference |
| SuperStore 14130 | review | **review** | PO-SS-005 | line_level: 1 -> 1, 1.00 | within_tolerance | r_po_found matched_without_reference |
| IQ Electronics scan | review | **review** | PO-IQ-2025-001 | line_level: 1 -> 1, 1.00 | within_tolerance | engine_floor, r_extraction_confidence low_confidence, r_po_found matched_without_reference |

Results per run: 15 before, 16 after (the new rule). No decision, PO match or triggered check changed. The labelled SYNTHETIC approve variant (24429 with PO-SS-002 edited in) still approves and now also writes one total-only `po_consumption` row (tested). With the older v4 replies pinned in `test_six_invoices.py`, IQ has no matched PO (no currency), so its line matching is `not_evaluable` (tested).

## The four synthetic line-item scenarios (`tests/pipeline/test_line_items_synthetic.py`)
Generated Northwind invoice as a PNG, the recorded-style Northwind reply edited per scenario and labelled `[SYNTHETIC LINE-ITEM SCENARIO ...]`, a test-only vendor and 3-line PO-5001 (demo seed untouched):

| Scenario | Line match | `r_po_line_price` | Decision |
|---|---|---|---|
| clean (Widget A 10 x 60, B 5 x 80 vs PO A/B/C) | line_level: 1 -> 1, 2 -> 2 (1.00 each) | within_tolerance | approve; ledger commit + one total-only `auto` consumption row |
| ambiguous (vs PO "Widget A (blue)" / "(green)" / B) | partial: line 1 ambiguous (0.86 vs 0.84), line 2 -> 3 | within_tolerance on line 2; line 1 skipped | approve (decision 3: line matching changes no decision yet) |
| bundled (one line "Goods as per purchase order PO-5001") | total_only, bundled hint, no_match | not_evaluable | approve (whole-PO match by reference unaffected) |
| price (Widget A billed 66.00 vs PO 60.00) | line_level, line 1 scored 0.93 | **price_above_po**: D 6.00 > A 0.60 | **review** (the only triggered check) |

## What changed (line-item stage 4: stored line matches and the picker's data)
- The act stage writes `invoice_line_matches` (one row per invoice line when a PO was matched; SPEC section 11 item 77); the act summary counts them.
- `GET /api/runs/{id}` has `line_matches` (per invoice line: status, automatic choice, top-3 candidate PO lines with text, prices, consumed/remaining; all PO lines; the PO split). `GET /api/pos/{id}` lines carry consumed/remaining quantity and amount, and `amounts` carries `consumed_by_lines` / `consumed_without_line`.
- Three table-set assertions extended for the new rows (`test_runner`, `test_stage_events` x2), one PO-detail equality extended.

## What changed (line-item stage 3: `r_po_line_price`)
- `app/engine/evaluators/po_line_price.py` (SPEC section 11 item 76), registered as the 14th builtin rule: params `pct` 1.0, `abs` 1.00, `mode lesser_of`, `direction above`, severity 1; outcomes `within_tolerance` / `price_above_po` / `price_below_po` (only with `both`) / `not_evaluable`.
- **One existing decision changed, on a hand-written (not real) fixture:** `tests/engine/test_pipeline_e2e.py::test_over_balance_within_and_beyond_tolerance` used an invoice that billed "Premium gadget" at 83.00 against the PO line's 80.00 only to push the total 15.00 over the balance. The new rule flags that (D 3.00 > A 0.80), so it is now review instead of approve. I re-based that test on an extra freight line (prices equal, still approve within tolerance) and added `test_a_unit_price_above_the_po_line_is_reviewed_even_within_the_total_tolerance`, which pins the original fixture's review. No real-invoice decision changed.
- Count/list updates only (13 -> 14 rules, 15 -> 16 results, the rule tables and trails gain `r_po_line_price`): `test_api`, `test_builtin_rules` (and its clean-invoice test now gives the context its line matches, as the match stage would), `test_pipeline_e2e`, `test_cli`, `test_digest_and_templates` ("14 checks passed"), `test_persist`, `test_runner`, `test_six_invoices`, `test_stage_events`, `test_config`, `test_real_invoices_e2e`.
- `add_missing_builtin_rules`: the migrate command, `serve` and the pipeline CLI insert builtin rules the database lacks (INSERT OR IGNORE, never touching an existing rule) and say which, so your migrated `data\app.db` gets `r_po_line_price`.

## What changed (line-item stage 2: the line matcher)
- `app/engine/line_matching.py` (SPEC section 11 item 75): description 0.60 (similarity or containment, item codes decisive), price 0.15, quantity 0.15, amount 0.10 against the PO line's remaining quantity/amount; matched / ambiguous / no_match / not_evaluable per line; modes line_level / total_only (+ bundled hint) / partial / not_evaluable. Config `LineMatchConfig` (`settings.line_match`).
- Runs in the match stage against the confidently matched PO only; result in `RunContext.line_matches` and a new `po_lines_matched` audit event; the match stage summary carries the mode and counts. No decision uses it.
- `POLineFact` gains `id`, `consumed_quantity`, `consumed_amount` (+ derived `remaining_*`); the loader sums line-assigned consumption only.
- New fixtures `tests/fixtures/real/*.v5.reply.json`: the six replies recorded live with extract-v5 on 2026-09-25 (copied from `data/recordings`; reply text only, no key). The six-invoice test keeps the v4 replies it pins; both are exercised.
- On all six (v5): each line `matched` to line 1 of its PO, score >= 0.93, mode `line_level`, decision unchanged (review). With v4, IQ has no matched PO (no currency) so it is `not_evaluable`, as expected.

## What changed (line-item stage 1: schema v2)
- `app/db/schema_v2.sql`: `po_consumption` and `invoice_line_matches` (SPEC section 5 and section 11 item 74). No existing table changed.
- `app/db/consumption.py`: `backfill_consumption` (one `legacy` row against the PO total per unallocated ledger entry), `consumption_problems` (the invariant), `record_consumption`.
- `app/db/migrate.py`: `python -m app.db.migrate [--db PATH]` backs up the file, then in ONE transaction creates the tables, backfills, verifies and sets version 2; any problem rolls back (tested: the tables and the version change are undone, the backup is kept). A v2 database is left alone.
- `init_db` creates v2 directly; `init_db`, `serve` and the pipeline CLI refuse a v1 database with the migrate command in the message. The seed loader backfills the seeded commits (demo: one `legacy` row of 1,500.00 on PO-SS-005; balances unchanged).
- The approve path now also writes one `po_consumption` row (against the PO total, `matched_by auto`) beside its ledger commit, in the same transaction.
- Existing tests adjusted (setup or table lists only): `test_db_init` (two more tables), `test_enum_drift` (three new CHECK lists mapped to `LedgerType`, `MatchedBy`, `LineMatchStatus`), `test_reset` and three pipeline setups (delete allocation rows before ledger rows), `tests/helpers.SEED_TABLES` (+ `po_consumption`), `test_stage_events` (the approve's act summary counts the consumption row).

## How to check it (PO stage 7, yours)
Window 1: `cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; pip install openpyxl; cd backend; python -m app.api.serve --replay ..\data\recordings --reset-demo` (use `--live` instead for real PO drafts). Window 2: `cd C:\Zamp_ai_Automation\frontend; npm run dev`. Open http://localhost:5173.
- **Why fix:** open any invoice result; the "Why" bullets are plain sentences, "Technical details" shows the ids; the review-queue item too.
- **Purchase orders tab:** the 6 demo POs with balances (PO-SS-005: 9,000.00 total, 7,500.00 balance); open one.
- **New PO > Form:** errors block Save, warnings do not; "New vendor..." says status new; save -> the PO's page ("Entered by: Form").
- **Describe in text / Upload a document:** on `--replay` these return "no recorded response" (there are no PO recordings), which shows the failure path; on `--live` they draft (about $0.01 per text, $0.02-0.03 per document) and nothing is saved until Save.
- **Multi-upload:** choose several invoices at once on Invoices; each gets its own row and decision. From a PO page, "Upload invoices" keeps you on the upload page with the "matching is automatic" note.
- Smoke check done here on a replay server (scratch DB): a form-saved PO appeared in the list with source manual; a text draft came back `replay_miss`; two uploaded invoices ran; PO-SS-001's page showed the review run and 5,338.08 awaiting review.

## What changed (PO stage 6: multi-invoice upload)
- Invoices screen accepts several files (drop or picker, cap `ui_max_files_per_upload` = 20, now also in `/api/health`). One `POST /api/runs` per file, in order; each is its own run in the existing queue with its own decision; a rejected file fails alone. A "This upload" list shows each file (waiting / uploading / queued / running / decision, matched PO, link to its run), polled every 1.5 s. A single file still goes straight to its live run view.
- From a PO page, "Upload invoices" opens the same upload with a note that matching is automatic; nothing about the PO is sent with the upload; rows that matched another PO say "(not this PO)".

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
- **Your stage-3 browser check of the dashboard.**
- **Your stage-4 browser check of review actions** (and whether you want the demo loader for the line-choice path).
- **Review this line-item build** before the follow-up (approve/reject + picker UI). Open questions for it: whether `partial` / `ambiguous` line matches should force review (decision 3 deferred it), and how the reviewer's line choice writes consumption.
- Your PO stage-7 browser check (and, when you want, the live PO-draft check).
- Confirm M4 stage 5 when you have checked it.

## Assumptions added to SPEC section 11
79 (dashboard). Earlier: 78 (review actions), 74-77 (schema v2, line matching, `r_po_line_price`, picker data), 72-73 (PO entry, PO drafting), 69-71 (M4), 61-68 (M3).

## Known risks or gaps
- Line matching: short descriptions one letter apart ("Widget A" vs "Widget B") score 0.88 on description alone; price, quantity and amount keep such a line far below a match (0.575 < 0.75 in the tests) and it is listed only as a low candidate. Real POs with near-identical names and identical prices would come out ambiguous, which is the safe side.
- Consumption per line is recorded only from the follow-up on; until then every approve is allocated to the PO total (`matched_by auto`, no line).
- PO drafting (text and document) has run only against scripted replies; the prompt has no live evidence until your live check. A replay server has no PO recordings, so on replay every draft comes back failed (`replay_miss`); use `--live` for real drafts.
- The "Why" parser depends on the template wording; the contract test fails first if it changes.
- The real explainer/drafter have never run live. Extraction varies between calls.
- The IQ manifest entry is still an unverified draft (`currency` should be `"INR"` when you verify it).
- Node here is 22.19; jsdom is pinned to 29 (jsdom 30 needs 22.22+). Without a declared Content-Length an upload is buffered before the size check.
