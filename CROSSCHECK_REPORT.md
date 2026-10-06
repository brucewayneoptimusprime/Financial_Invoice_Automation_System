# Cross-check documents (report only): build report (C0-C1 done, C2-C5 not built)

Branch `feature/cross-check`, created from `deploy` at `60ae234`. `deploy`, `master` and every other branch are unchanged, and nothing has been pushed.

**The feature is not finished.** Stages C0 and C1 are built and committed. The build stopped after C1, as instructed, because the live schema check was refused by the API with an authentication error. Stages C2 to C5 have not been started, so there is no route and no screen yet, and the feature cannot be tried in the browser.

- No schema change, no new dependency, no database write anywhere in the new code.
- No `.env` value or token was printed or logged. The live test prints tokens and cost only.
- Live spend for this feature: **$0.00** of the authorized $0.10 (every call was refused before any tokens were used).

## 1. Commits

| Stage | Commit | State | What |
|---|---|---|---|
| plan | `9fb069d` | done | `CROSSCHECK_PLAN.md`. Approved 2026-10-07: 1 yes, 2 yes, 3 every non-rejected invoice with its status named, 4 exact, 5 each document alone, 6 option A, 7 none, 8 yes, 9 PDF/PNG/JPG only, 10 yes. |
| C0 | `c761747` | done | **Baseline.** Suites recorded; the six real invoices pinned. |
| C1 | `91aeece` | done | **Wire schema, prompt, reader.** See below. |
| live check | none | **blocked** | Refused with HTTP 401/403 on 2026-10-07, twice (see section 4). |
| C2 | none | **not started** | `facts.py`, `compare.py`: relevance and the ten difference rows. |
| C3 | none | **not started** | `service.py`, the two routes, the read-only connection, SPEC sections 7 and 11. |
| C4 | none | **not started** | `CrossCheck.tsx`, the API client, frontend fixtures and tests. |
| C5 | this file only | **not done** | The README section is not written (there is nothing to document yet). This report is the only C5 item that exists. |

**C0:**
- Baseline on this branch before any code: backend 2599 passed, 4 deselected; frontend 179 passed; `tsc --noEmit` clean.
- `backend/tests/crosscheck/test_crosscheck_regression.py`: the six real invoices give the decision, matched PO and triggered rules of `deploy`.
- The "run a cross-check, then compare again" half of that test belongs to C3 and is not written.

**C1:**
- `backend/app/crosscheck/wire.py`: the `crosscheck-v1` schema (4 objects, 24 properties, no unions, no nulls, every property required) and its converter. Only the schema's keys are read; anything else in a reply is ignored.
- `backend/app/crosscheck/prompts.py`: a separate prompt, so `extract-v5` and `po-draft-v1` and their recordings are untouched. Its fingerprint is pinned by a test.
- `backend/app/crosscheck/reader.py`: one document to facts.
  - One repair retry, then a failed result with a code and message. It never raises.
  - Amounts, quantities, dates and the currency are normalised with the existing helpers; a value that does not parse is dropped and noted.
  - Every value is grounded with the existing `ground_item`. `value_mismatch`, `not_found` and `no_source` mark it not confirmed; a scan with no text layer stays usable and is marked `unavailable`.
  - The existing reader-instruction scan flags a hostile document.
- `backend/app/config.py`: six settings (`crosscheck_enabled`, `crosscheck_max_documents`, `crosscheck_max_output_tokens`, `crosscheck_min_line_share`, `crosscheck_typical_cost_usd`, `crosscheck_tmp_dir`). Nothing reads the first, second, fourth, fifth or sixth yet.
- `backend/tests/crosscheck/test_c1_reader.py`: 26 tests with scripted doubles.
- `backend/tests/crosscheck/test_live_crosscheck.py`: the two live-marked tests, sharing one tracker capped at $0.10.

## 2. Test counts (run fresh on 2026-10-07, at `91aeece`)

| Suite | Command | Result |
|---|---|---|
| Backend | `python -m pytest -q -W error` from `backend\` | **2626 passed**, 0 failed, 6 deselected (302 s) |
| Frontend | `npx vitest run` from `frontend\` | **179 passed** (17 files) |
| Types | `npx tsc --noEmit` | clean, exit 0 |
| Build | `npx vite build` | built, exit 0 (JS 366.53 kB, CSS 31.20 kB) |
| Live | `python -m pytest -m live -s tests/crosscheck/test_live_crosscheck.py` | **2 failed**, both with the authentication error |

- Backend: 2599 before, plus 27 new (1 regression, 26 reader). The 6 deselected are the 4 existing live tests and the 2 new ones.
- Frontend: unchanged at 179, because no frontend code has been written.

## 3. Changed existing assertions

None. No existing test file was edited; the diff against `deploy` in `backend/tests/` and `frontend/src/test/` only adds files under `backend/tests/crosscheck/`.

## 4. Live schema check and measured cost

**Not passed, and no cost has been measured.**

- Both calls (schema acceptance on a typed delivery note; one real one-page PDF, `superstore_10963.pdf`) were refused with "The API rejected the API key or its permissions (HTTP 401/403)". Tokens in 0, tokens out 0, cost $0.000000.
- This happened on the first attempt after C1 and again when re-run for this report.
- The key is read from `.env` at the repo root. No `ANTHROPIC_API_KEY` variable is set in the shell, so nothing overrides the file. A key is loaded from the file, and that key is what the API refuses.
- So it is **not known** whether the API accepts the `crosscheck-v1` schema. The schema is smaller than the invoice schema the API already accepts (24 properties against 29, no unions), which makes acceptance likely, but that is an expectation and not a result.
- The only cost figures are the plan's estimates: about $0.014 for a one-page document, about $0.055 for a ten-page one, a hard ceiling of $0.25 per document.

## 5. Deviations from the plan

| Plan | What happened | Why |
|---|---|---|
| Build C0 to C5 without stopping | Stopped after C1 | The stop condition you set: an authentication error on the live call. |
| Live check right after C1 | Run twice, refused both times | The API key. |
| `crosscheck_max_output_tokens` 2000 | 3000 | A document with around 50 lines would be cut off at 2000. The worst-case reservation per document is still well under the $0.25 ceiling. |
| `crosscheck_tmp_dir` not in the plan | Added (default: the system temp directory) | So the "temp folder is gone" test in C3 has a folder to look at. |
| C5 report "when done" | Written now, with the feature unfinished | You asked for it. |

## 6. Unresolved items

1. **The API key.** Nothing live can run until `.env` holds a working `ANTHROPIC_API_KEY`. The existing line has a space before the `=`; it is still read, so the space is not the cause, but it is worth tidying when you replace the key.
2. **The live schema check and the measured cost** (section 4).
3. **C2 to C5 are not built.** Still to do: relevance and differences, the two routes behind `ACCESS_TOKEN`, the read-only connection and the no-writes tests, the caps and the budget pre-check, the offline and replay messages, the UI section, the second half of the six-invoice regression, SPEC section 7 and section 11, and the README section.
4. **Gates not run**, because the code they test does not exist: matching document, each difference type, not related, vendor only, hostile document at the API level, no database writes, the access gate, caps, empty and unreadable files, modes, the frontend tests for the section.
5. **`PROJECT_STATE.md`** at the repo root is untracked. It was there before this work started and I have not touched it.

## 7. Trying it in the browser

**Not possible yet.** There is no cross-check section on the PO page and no `/api/pos/{id}/crosscheck` route, because C3 and C4 are not built. The steps below are what you can do today, followed by the start commands that will apply once the feature is finished.

### 7.1 What you can check today

From `C:\Zamp_ai_Automation\backend`, with the virtual environment active:

```
python -m pytest -q tests/crosscheck
```

Expected: `27 passed, 2 deselected`. No network, no cost.

After putting a working key in `.env`:

```
python -m pytest -m live -s tests/crosscheck/test_live_crosscheck.py
```

Expected: `2 passed`, with two `[live]` lines showing `status=ok`, the tokens and the cost of each call. Total cost should be about $0.03 and cannot exceed $0.10. If it prints `code=auth` again, the key is still being refused. If it prints `code=schema_rejected`, the API refused the schema and I need to shrink it before going on.

### 7.2 Start commands for later (your own database, not reset)

These are the existing commands and they already work for the rest of the application. The point to remember is to **leave out `--reset-demo`**: that flag wipes the database, including a stored Gmail connection.

Terminal 1, backend:

```
cd C:\Zamp_ai_Automation
.\.venv\Scripts\Activate.ps1
cd backend
python -m app.db.migrate
python -m app.api.serve --live
```

- `python -m app.db.migrate` brings `data\app.db` up to schema v4 if it is older, after taking a backup. It does nothing if the database is current. This feature adds no table, so it needs no migration of its own.
- `python -m app.api.serve --live` uses the database from your configuration (`data\app.db` by default) as it is. To point at a different file, add `--db PATH`.
- If the server exits with code 3, the API key is not configured.

Terminal 2, frontend:

```
cd C:\Zamp_ai_Automation\frontend
npm run dev
```

Then open http://localhost:5173.

### 7.3 What the finished feature should show

This is the plan, not something I have observed. I will replace it with tested steps, naming the PO and the files, when C4 is done.

- Open a PO that has at least one invoice matched to it, and scroll to the last section, "Cross-check documents (report only)".
- The label "Report only: nothing here changes the PO, its invoices, the ledger or any decision." is always visible.
- Before Analyze: an estimated cost per document, the $0.25 per-document ceiling and the budget left. Nothing is uploaded until you click Analyze.
- After Analyze: the actual cost, then one block per document with Related or Not related and its four reasons, then "Differences found" with both values and where each comes from, or "No differences found".
- A file that cannot be read shows its own message; the other documents still report.

## 8. To continue

Put a working key in `.env` and tell me to continue. I will re-run the live check first, then build C2 to C5 in order, and replace this report with the complete one.
