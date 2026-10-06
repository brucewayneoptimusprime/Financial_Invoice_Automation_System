# Cross-check documents (report only): build report (C0-C5)

Branch `feature/cross-check`, created from `deploy` at `60ae234`. `deploy`, `master` and every other branch are unchanged, and nothing has been pushed.

All six stages are built and committed, and the live schema check passed.

- No schema change, no new dependency, no new paid service.
- The feature writes nothing to the database. Its routes open the database read-only.
- No `.env` value or token was printed or logged. The live test prints tokens and cost only.
- Live spend for this feature: **$0.0566** of the authorized $0.10.
- One thing I could not do: click through the screen in a real browser (the browser extension was not connected). Section 7 says exactly what was and was not tested.

## 1. Commits

| Stage | Commit | What |
|---|---|---|
| plan | `9fb069d` | `CROSSCHECK_PLAN.md`. Approved: 1 yes, 2 yes, 3 every non-rejected invoice with its status named, 4 exact, 5 each document alone, 6 option A, 7 none, 8 yes, 9 PDF/PNG/JPG only, 10 yes. |
| C0 | `c761747` | **Baseline.** Suites recorded; the six real invoices pinned. |
| C1 | `91aeece` | **Wire schema, prompt, reader.** See below. |
| (interim) | `178fd1e` | The interim report written while the API key was being refused. This file replaces it. |
| C2 | `7067a67` | **Facts and comparison.** Also records the live check passing. |
| C3 | `ae3bfd6` | **Service and routes, SPEC.** See below. |
| C4 | `fc63bcb` | **UI.** See below. |
| C5 | the commit that adds this file | **Docs.** README section, the notice-wording fix found by the live run, STATUS, this report. |

**C0:**
- Baseline on this branch before any code: backend 2599 passed, 4 deselected; frontend 179 passed; `tsc` clean.
- `tests/crosscheck/test_crosscheck_regression.py`: the six real invoices give the decision, matched PO and triggered rules of `deploy`.

**C1:**
- `app/crosscheck/wire.py`: the `crosscheck-v1` schema (4 objects, 24 properties, no unions, no nulls, every property required) and its converter. Only the schema's keys are read.
- `app/crosscheck/prompts.py`: a separate, fingerprint-pinned prompt. `extract-v5` and `po-draft-v1` and their recordings are untouched.
- `app/crosscheck/reader.py`: one document to facts.
  - One repair retry, then a failed result with a code and message. It never raises.
  - Values are normalised with the existing helpers; one that does not parse is dropped and noted.
  - Every value is grounded with the existing `ground_item`; an unsupported value is marked not confirmed.
  - The existing reader-instruction scan flags a hostile document.
- Six `crosscheck_*` settings in `app/config.py`.

**C2:**
- `app/crosscheck/facts.py`: a read-only connection (`mode=ro` plus `query_only`), the PO, its lines, vendor and aliases, and the invoiced quantity per PO line.
- `app/crosscheck/compare.py`: pure functions.
  - Line ties by description only.
  - Four relevance signals, each reported with both values.
  - Difference rows 1 to 9; absent PO lines as information; a reason for everything not compared.

**C3:**
- `app/crosscheck/service.py`: per-document failures, the all-or-nothing budget pre-check, cost totals.
- `app/api/routes_crosscheck.py`: `GET` and `POST /api/pos/{id}/crosscheck`. The temp folder is removed in a `finally`.
- The second half of the six-invoice regression.
- `SPEC.md`: a "Document reader" row in section 7 and item 99 in section 11. Sections 4 to 6 are untouched.

**C4:**
- `frontend/src/components/CrossCheck.tsx`, mounted as the last section of the PO page.
- `frontend/src/api.ts`, `types.ts`, `styles.css`: the client functions, types and neutral styles.
- Fixtures recorded from the real endpoints by `python -m tests.crosscheck.frontend_fixtures`, with a drift test.

## 2. Test counts (run fresh after the last code change)

| Suite | Command | Result |
|---|---|---|
| Backend | `python -m pytest -q -W error` from `backend\` | **2672 passed**, 0 failed, 6 deselected |
| Frontend | `npx vitest run` from `frontend\` | **193 passed** (18 files) |
| Types | `npx tsc --noEmit` | clean, exit 0 |
| Build | `npx vite build` | built, exit 0 (JS 377.14 kB, CSS 31.89 kB) |
| Live | `python -m pytest -m live -s tests/crosscheck/test_live_crosscheck.py` | **2 passed** |

- Backend: 2599 before, plus 73 new (2 regression, 26 reader, 26 comparison, 18 API, 1 fixture drift). The 6 deselected are the 4 existing live tests and the 2 new ones.
- Frontend: 179 before, plus 14 new in `crossCheck.test.tsx`.

**What the new tests cover, against the plan's list:**

| Plan item | Covered by |
|---|---|
| Scripted model doubles | every test except the two live ones |
| A matching document produces no differences | C2 and C3, plus the frontend |
| Each difference type from a crafted document | C2 tests 1 to 10, one per row |
| An unrelated document is reported as not related | C2, C3, frontend |
| A hostile document cannot change anything | C1 (an obedient model's extra keys are ignored), C2 (same differences with and without the hostile text), C3 (database identical) |
| No database writes | C3: the row count of every table and the SHA-256 of the database file, before and after; a structural test on the source; a test that the connection refuses an `UPDATE` |
| The access gate | C3: 401 without the token on both routes, 200 with it |
| Caps | C3: 6 files, 0 files, the cap as a setting, an oversize file, the budget pre-check, a per-document ceiling |
| Empty and unreadable files | C3: 0 bytes, an executable, a password-protected PDF, a blank PDF, a Word file |
| The six-invoice regression | two tests; the second runs cross-checks between and after the six |
| One live-marked test | two, see section 4 |

## 3. Changed existing assertions

None. No existing test file was edited. The diff against `deploy` under `backend/tests/` and `frontend/src/test/` adds 14 files, all named `crosscheck` or `crossCheck`, and changes no other.

One thing worth knowing: mounting the new section on the PO page did not disturb the existing PO page tests. Their mocks answer the new `GET` with unrelated JSON, and the section renders nothing unless the answer is a real cross-check answer.

## 4. Live schema check and measured cost

**Passed.** The API accepted `crosscheck-v1` as a strict structured-output schema on the first attempt, with no repair retry in any call.

| Call | Tokens in | Tokens out | Cost |
|---|---|---|---|
| Schema check: a typed delivery note, text only | 2,703 | 548 | $0.010886 |
| Real PDF `superstore_10963.pdf`, one page, text and image (live test) | 5,275 | 475 | $0.015300 |
| Real PDF `invoice_Scot Wooten_10963.pdf`, one page (through the running server) | (10,564 for both) | (925 for both) | $0.015270 |
| Real PDF `invoice_Maria Zettner_24429.pdf`, one page (through the running server) | | | $0.015108 |
| **Total spent** | | | **$0.056564** |

- **Measured cost per document: about $0.015** for a one-page PDF with a text layer (three readings: $0.0153, $0.0153, $0.0151).
- The plan estimated $0.014. The figure shown before Analyze is $0.02 per document (`CROSSCHECK_TYPICAL_COST_USD`), which is a fair round-up.
- Earlier, before the key was replaced, four calls were refused with an authentication error at no cost.
- Not measured: a multi-page document, and a scan with no text layer. Both will cost more per document; the $0.25 ceiling still applies.

## 5. Deviations from the plan

| Plan | What was built | Why |
|---|---|---|
| `app/crosscheck/models.py` with Pydantic response models | Not created; the report is built as plain dictionaries | The shape is pinned by the fixture drift test and the frontend types instead. Say if you want the models. |
| `crosscheck_max_output_tokens` 2000 | 3000 | A document with around 50 lines would be cut off at 2000. |
| No temp-folder setting | `crosscheck_tmp_dir` (default: the system temp directory) | So a test can prove the folder is empty afterwards. |
| Tie = top score at least 0.75 with no runner-up within 0.10 | The same, plus: an exact description match beats a near one | Otherwise "Widget A blue" on a PO that also has "Widget B blue" would be ambiguous even when the text is identical. |
| Quantity vs invoiced | A PO line with no invoice quantity tied to it is listed as "not compared", never compared with zero | Claiming "invoiced: 0" would be a statement the data does not support. |
| More than 5 files answers 422 | 6 files answer 422 `too_many`; 7 or more are stopped earlier by the form parser with a 400 | The parser's own cap is set one above the limit so that a huge upload is not read in full. Neither is a 500, and nothing is sent to the model. |
| C4 gate: "a manual check in the browser" | Not done as a click-through; replaced by a live request through the dev server (section 7.1) | The browser extension was not connected. |
| The interim report | Committed as `178fd1e`, replaced by this file | You asked for it while the key was blocked. |
| Notice wording | Fixed in C5 | The live run showed "Left out: currency '$' was mapped to USD", which was wrong: nothing was left out. It now reads "Currency '$' was mapped to USD by configuration." |

## 6. Unresolved items

1. **No click-through in a real browser by me.** The screen is covered by 14 component tests and the request it sends was run live, but I have not seen the page render in Chrome. Please treat section 7.2 as the first real look.
2. **No real delivery note or goods receipt was used.** The repository has only invoices. The live runs used two real invoices as the "anything else" kind of document; every difference type is proven only with generated documents and a scripted model.
3. **Scans were not run live.** PNG, JPG and image-only PDFs are covered with a scripted model only.
4. **An invoice attached as a document will usually show a total difference.** The expected total is built from PO line prices, so shipping, tax or a discount on the document shows up as a difference (section 7.2 shows this). That is the exact comparison you chose; it is not a fault, but it will look like one until read.
5. **Related by vendor only is noisy by design.** A document for another PO of the same vendor is related, and its lines show as items not on this PO.
6. **Long documents.** Above roughly 60 item lines the reply may hit the 3000-token cap; the document then fails with a clear message after one retry, and both attempts are paid for. `CROSSCHECK_MAX_OUTPUT_TOKENS` raises it.
7. **Nothing is stored.** A report is gone on reload, and its cost is in the server log and the session budget but not on the dashboard. You chose this (question 7).
8. **Not deployed, and not tested deployed.** Nothing was pushed, so Render and Vercel do not have it.
9. **The README "Testing" paragraph still quotes the `deploy` counts** (2,599 and 179). I left it, because it names that branch. It should be updated when this branch is merged.
10. **`PROJECT_STATE.md`** at the repo root is still untracked. It was there before this work and I have not touched it.

## 7. Trying it in the browser, in live mode, on a scratch database

### 7.1 What I tested, so you know how far to trust the steps

- **Tested, live:** a scratch database made with `DATA_DIR` and `python -m app.db.reset --demo`; the backend started with `--live` on it; the frontend dev server; then the request the page sends (`POST /api/pos/1/crosscheck` with the two files below), sent through the dev server at `http://127.0.0.1:5173`. The answers in 7.3 are what came back.
- **Tested:** the database file's SHA-256 was the same before and after that analysis, and the hash of your main `data\app.db` was the same before and after everything.
- **Tested:** the PowerShell form of the `DATA_DIR` commands below (set, reset, remove).
- **Not tested by me:** clicking the buttons in Chrome. The buttons, labels and report layout are covered by component tests only.
- The model's wording can vary slightly between runs (for example "Invoice" or "INVOICE" as the document's title). The numbers should not.

### 7.2 Steps

**Terminal 1, backend (PowerShell):**

```powershell
cd C:\Zamp_ai_Automation
.\.venv\Scripts\Activate.ps1
$env:DATA_DIR = "$env:TEMP\crosscheck-scratch"
cd backend
python -m app.db.reset --demo
python -m app.api.serve --live
```

- `$env:DATA_DIR` lasts only for this terminal window. It moves the database, run folders, uploads and PO drafts into the scratch folder.
- `python -m app.db.reset --demo` must print a path that ends in `crosscheck-scratch\app.db`. **If it prints `C:\Zamp_ai_Automation\data\app.db`, stop**: the variable is not set, and a reset there would wipe your main database and your Gmail connection.
- The server prints `LIVE MODE` and `Database: ...crosscheck-scratch\app.db`.
- In this scratch database Gmail shows as not connected. Your real connection lives in `data\app.db` and is not touched.

**Terminal 2, frontend:**

```powershell
cd C:\Zamp_ai_Automation\frontend
npm run dev
```

**In the browser:**

1. Open http://localhost:5173/pos/1. This is **PO-SS-001** (SuperStore, one line: Hewlett Fax Machine, quantity 4 at 1285.44).
2. Scroll to the last section, **Cross-check documents (report only)**.
3. Click **Choose documents** and pick these two files from `C:\Zamp_ai_Automation\data\invoices`:
   - `invoice_Scot Wooten_10963.pdf`
   - `invoice_Maria Zettner_24429.pdf`
4. Click **Analyze 2 documents**. It takes about 10 seconds and costs about $0.03.

**Afterwards, to put things back:**

```powershell
# in terminal 1: Ctrl+C to stop the server, then
Remove-Item Env:DATA_DIR
Remove-Item -Recurse -Force "$env:TEMP\crosscheck-scratch"
```

Closing the terminal window also removes the variable. Your normal start command then uses `data\app.db` again, exactly as before.

### 7.3 What each part should show

**Before Analyze:**

| Part | Should show |
|---|---|
| Label | "Report only: nothing here changes the PO, its invoices, the ledger or any decision." |
| Limits | "Up to 5 files · PDF, PNG, JPG · at most 20 MB each" |
| Staged files | the two file names, each with a Remove button |
| Cost before | "Estimated cost: about $0.02 per document, so about $0.04 for 2 documents. Never more than $0.25 per document. Budget left this session: $5.00." |
| Network | nothing has been uploaded yet |

**After Analyze:**

| Part | Should show |
|---|---|
| Cost after | "This analysis cost $0.0304 (2 of 2 documents analysed, by Claude)." give or take a fraction of a cent, followed by the report-only label again |
| Budget | the "Budget left" figure drops to about $4.97 |

**Document 1, `invoice_Scot Wooten_10963.pdf`:**

| Part | Should show |
|---|---|
| Heading | **Related**, "Invoice", 1 page, about $0.0153 |
| PO number | no: "The document mentions no purchase-order number." |
| Invoice number | no: it mentions 10963, and no invoice is matched to this PO in a fresh demo database |
| Vendor | yes: SuperStore is the same name |
| Lines | yes: 1 of 1 line matches a line of this PO |
| Differences found (1) | **Document total**: 5338.08 on the document (page 1, "Total: $5,338.08") against 5141.76, the sum of the expected amounts from this PO's quantities and prices. The gap is the invoice's shipping charge. |
| Not compared | "Quantity on PO line 1 against invoices: no invoice is matched to this PO." |
| Notice | "Currency '$' was mapped to USD by configuration." |
| What was read | one line: Hewlett Fax Machine, quantity 4, unit price 1285.44, amount 5141.76, tied to PO line 1. Quantity, price and amount agree with the PO, so none of them is listed as a difference. |

**Document 2, `invoice_Maria Zettner_24429.pdf`:**

| Part | Should show |
|---|---|
| Heading | **Related by vendor only**, 1 page, about $0.0151 |
| Vendor | yes; PO number, invoice number and lines: no (0 of 1 line matches) |
| Differences found (1) | **Item not on the PO**: "Hon Rocking Chair, Black, quantity 4, unit price 461.48, amount 1845.94" against "No PO line has a similar description" |
| PO lines not on this document | Line 1: Hewlett Fax Machine, ordered quantity 4 |
| Not compared | "Document total: not every line of the document is tied to a PO line." |

**Then check that nothing changed:** the stats at the top of the PO page (Total 6,000.00, Committed 0, Balance 6,000.00), "No invoice has been matched to this PO yet" and the empty ledger are exactly as before. Reload the page: the report is gone, because nothing was stored.

### 7.4 Optional extra checks (not run live by me; covered by tests)

- **Invoiced quantity.** First process `invoice_Scot Wooten_10963.pdf` on the Invoices page (about $0.03 more). It is held for review against PO-SS-001. Then cross-check the same file: the Invoice number reason should turn to yes, and the quantity is compared with "10963 (in review): 4", with no difference.
- **A bad file.** Add an empty file or a Word document next to a good one: it shows "Not analysed" with its reason, and the good one still reports.
- **Offline.** Start the backend with `--offline` instead of `--live`: the section says "Offline mode: no model is available to read documents." and the buttons are disabled.
