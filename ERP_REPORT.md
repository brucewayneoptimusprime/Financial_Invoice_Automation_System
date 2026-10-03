# Simulated ERP purchase-order feed: build report (R1-R4)

Branch `feature/erp-feed`, created from `feature/settings` at `f211286`. `master` (`6efd185`), `feature/gmail-integration` (`81cab06`), `feature/po-export` (`f4a02dc`) and `feature/settings` (`f211286`) are unchanged, and nothing has been pushed.

- No model call, no Google call, no new dependency and no schema change.
- No `.env` value or token was printed or logged. The smoke server ran with an empty `ACCESS_TOKEN` on a scratch database, and its log contains no token-like text.

## 1. Commits

| Stage | Commit | What |
|---|---|---|
| plan | `cde5407` | `ERP_PLAN.md`. Approved: 1-5 and 7-9 as recommended; 6: a look-alike PO number is a problem. |
| R1 | `17a492f` | **Source, adapter, preview.** See below. |
| R2 | `9270cd1` | **Routes and import.** See below. |
| R3 | `91fae69` | **UI.** See below. |
| R4 | the commit that adds this file | **Docs.** README section "Simulated ERP purchase-order feed" and a known limitation; STATUS; this report. |

**R1:**
- `data/erp_feed_sample.json`: 13 entries.
- Settings: `ERP_FEED_ENABLED`, `ERP_FEED_PATH`, `ERP_FEED_MAX_BYTES`, `ERP_MAX_POS_PER_SYNC`, `ERP_MAX_IMPORT_PER_ACTION`.
- `app/erp/source.py`: size cap; JSON parsed with Decimal, so no floats.
- `app/erp/adapters/`:
  - the `ERPAdapter` contract and `FeedPO`;
  - `SimErpV1`;
  - a registry keyed by the envelope's `format`.
- `app/erp/preview.py`: classifies each PO as new / exists / problem, using the PO form's own `validate_po`. Writes nothing.

**R2:**
- `GET /api/erp/preview` and `POST /api/erp/import`.
- `app/erp/importer.py`:
  - all refusals happen before anything is written;
  - each pick is classified again against the current database;
  - saves go through `save_po`;
  - each pick reports imported / skipped_exists / refused.
- Provenance is stored in `meta`.
- The structural test now allows the importer as a caller of `save_po`.
- SPEC §11 item 97 added; item 72 amended.

**R3:**
- "Sync from ERP (simulated)" on the PO list.
- `/pos/erp-sync`: the preview with three groups and a confirm step.
- "Simulated ERP feed" shown in:
  - the Entered column;
  - "Where this PO came from";
  - the PO exports (summary and Full).
- Fixtures recorded from the real endpoints, with a drift test.

## 2. Test results (fresh run on the final tree, 2026-10-03)

**Backend:** `python -m pytest -W error` gave **2594 passed, 0 failed, 4 deselected** (the `live` tests).

| Stage | New tests | Total |
|---|---|---|
| before | | 2537 |
| R1 | +32 | 2569 |
| R2 | +23 | 2592 |
| R3 | +2 (fixture drift, export wording) | 2594 |
| R4 | 0 | 2594 |

**Frontend:**
- `npx vitest run`: **173 passed** in 16 files. `src/test/erpSync.test.tsx` is new, with 13 tests.
- `npx tsc --noEmit`: clean.
- `npx vite build`: OK.

**Real-server smoke test.** `serve --replay` on a scratch demo database in the scratchpad (`DATA_DIR`), port 8766:
- `/health` reported schema 4.
- The preview returned 5 new, 1 exists and 7 problems from `erp_feed_sample.json` (13 POs).
- Importing the 5 new POs plus `PO-SS-005` and `po-ss-002` gave 5 imported, 1 skipped_exists and 1 refused.
- A second preview returned 0 new, 6 exists and 7 problems. Importing the same 5 again gave 0 imported and 5 skipped.
- The imported PO 4500012001 had `source = erp`, feed `erp_feed_sample.json` and a total of 486.00.
- The list's Entered sources were `erp` and `seed`. The CSV export contained "Simulated ERP feed".
- An unconfirmed import returned 400 `not_confirmed`.
- Your `data\app.db` was only read for its schema version (4) and was not touched.

**Six real invoices** (`test_the_six_real_invoices_are_unchanged_before_and_after_importing_the_whole_feed`):
- The six invoices were uploaded on two demo databases: one as is, one after importing all five new sample POs.
- Every field compared is identical on both: decision, run status, matched PO, match status, triggered checks, 16 rule results, total, file hash, line mode, review item and ledger. All six remain matched to their own PO (PO-SS-001..005 and PO-IQ-2025-001). The "before" side also equals the known expected table.
- No sample PO had to be changed for this.
- Sample POs for SuperStore and Electronics Mart now appear at the bottom of some candidate lists on vendor evidence alone; see deviation 3.

## 3. Existing test assertions changed

**R2:** one.

1. `tests/po/test_po_save.py::test_save_po_is_called_from_exactly_one_route_and_only_store_writes_pos`.
   - **Before:** the files that call `save_po(` were `["routes_po.py"]`, and `routes_po.py` called it exactly once.
   - **After:** the callers are `["api/routes_po.py", "erp/importer.py"]`, and each calls it exactly once.
   - **Reason:** the approved plan routes ERP imports through the single PO writer, which makes the importer a second, deliberate caller.
   - The other half of the test (only `po/store.py` and `db/seed.py` contain `INSERT INTO purchase_orders` / `po_lines`) is unchanged. The importer writes nothing itself.

**R1, R3, R4:** none. The PO list, PO page and export tests pass unchanged; the new Entered wording is an added mapping.

## 4. Deviations from ERP_PLAN

1. **`ERP_FEED_ENABLED=false` does not hide the button.**
   - The plan said the button would be hidden. The frontend has no endpoint that reports server settings, so the button stays.
   - The sync screen shows the server's 404 message, "The simulated ERP feed is switched off."
   - Both routes answer 404 as planned. SPEC item 97 says so.
2. **Look-alike detection uses the existing normaliser** (`normalize_identifier`), per your decision 6. "Look-alike" therefore means equal after case and punctuation are removed and leading zeros in digit runs are dropped: `po-ss-002`, `PO SS 002` and `POSS002` all count. Look-alike PO numbers inside the feed itself count as one "duplicate in the feed".
3. **Candidate lists gain the sample POs.**
   - After an import, the extra POs for an existing vendor appear at the bottom of that vendor's invoices' candidate lists, with a low score from vendor evidence only.
   - Decisions and matches are unchanged (tested). The regression compares everything except the candidate list.
4. **The exports were updated too.** The PO export's summary Entered column and its Full provenance now say "Simulated ERP feed" and list the feed facts. Without this, the summary would have said "Erp", unlike the screen. This was not in the plan; it is tested.
5. **A zero quantity is also a problem**, not only a negative one, per the plan's "the ERP contract says quantity > 0". The manual form is unchanged.
6. **ERP line numbers.** ERP line numbers (10, 20, …) and units of measure are kept in `meta`, as decided. The PO's own line numbers are 1..n, as with the form.

## 5. Unresolved items

- **I did not look at the screens in a browser.** The UI is covered by vitest with fixtures recorded from the real endpoints, and the API by the smoke test. Layout and phone width are yours to check (section 6).
- **Two concurrent imports that both create the same new vendor** could create that vendor twice, because vendor resolution happens before `save_po`'s own transaction. A repeated PO is still impossible (unique key, tested). This is unlikely in a single-user demo and is not fixed.
- **Your own database may give different counts.** The preview classifies against whatever is in it: a PO you created with one of the sample numbers shows as "exists", and a deleted seed PO changes the "exists" and look-alike rows. Section 6 lists the counts on a clean demo database.
- **No scheduled sync, no ERP acknowledgement, no updates to existing POs.** All by design; see README known limitations.
- `PROJECT_STATE.md` is still untracked, as before.

## 6. How to check it in the browser

Your `data\app.db` is already at schema v4, and this feature adds no table, so **no migrate step is needed**.

**Terminal 1 (backend):**
```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1
cd backend
python -m app.api.serve --replay ..\data\recordings     # or --offline: the ERP feed needs no model either way
```

**Terminal 2 (frontend):**
```powershell
cd C:\Zamp_ai_Automation\frontend; npm run dev
```

Open **http://localhost:5173/pos**.

1. **The button.** At the top right of Purchase orders, next to "New purchase order", is **Sync from ERP (simulated)**. Hovering it says "Simulated ERP (demo): a bundled sample feed…". Click it.
2. **The preview** (`/pos/erp-sync`). The title has a "Simulated ERP (demo)" chip. The line below names `erp_feed_sample.json`, SIMERP, format `simerp.po-feed/v1` (adapter simerp-v1) and 13 purchase orders. Nothing is saved yet. On a clean demo database the groups show:

   **New (5)** — tick-boxes, none ticked:
   - **4500012001**: existing: SuperStore (by name), USD 486.00, 12 EA LED desk lamp.
   - **4500012002**: existing: SuperStore (by alias, because the feed says "Super Store"), 2 lines.
   - **4500012003**: existing: Electronics Mart India Limited (by tax ID), INR 1,840.00.
   - **4500012004**: new vendor: Northwind Office Supplies Ltd, "status new", with the form's warning that it will be created with status new.
   - **4500012005**: the same new vendor, in EUR.

   **Already exists (1):**
   - **PO-SS-005**: "is already stored and is skipped; it is never changed.", with **Open PO-SS-005**. The feed's version has a total of 1.00; the stored PO keeps its own values, invoice and ledger.

   **Has problems (7)** — no tick-boxes:

   | PO | Reason shown |
   |---|---|
   | po-ss-002 | "PO number po-ss-002 looks like existing PO-SS-002.", with **Open existing PO-SS-002** |
   | 4500012006 | "Currency is required (it is never guessed)." |
   | 4500012007 | "Line 1: 5 x 19.99 = 99.95, not 109.95." |
   | 4500012008 | "Line 1: quantity cannot be negative (-3)." |
   | 4500012009 | Listed twice, each "appears more than once in the feed; none of its copies is imported." |
   | 4500012010 | "The PO is CANCELLED in the ERP, not released…" |

3. **Import.** Tick **4500012001** and **4500012004**. The button reads **Import 2 purchase orders**. Click it: a confirmation appears, "Save 2 purchase orders from the Simulated ERP (demo)?". Click **Confirm import**.
4. **The result.** "2 imported." Each PO number is a link, and 4500012004 says "new vendor created (status new)". The preview reloads: **New (3)**, **Already exists (3)**.
5. **Provenance.**
   - Click **4500012001**. Under **Where this PO came from**: Entered by **Simulated ERP feed** with the "Simulated ERP (demo)" chip, Entered, Feed `erp_feed_sample.json`, Synced, ERP status RELEASED, Buyer reference "REQ-7781 / J. Rao", Units of measure "line 1: EA".
   - Back on Purchase orders, the **Entered** column for the imported POs says **Simulated ERP feed**.
   - Optional: **Export** on that PO → Full → CSV. The file says "Entered by,Simulated ERP feed".
6. **Re-sync.** Open **Sync from ERP (simulated)** again. The two imported POs are now under **Already exists**. Tick the remaining three new POs and import them: Northwind is reused (not created again) for 4500012005.
7. **Vendor check.** The new vendor Northwind Office Supplies Ltd appears in the vendor choice on **New purchase order**. Its invoices would go to review until the vendor is approved, as for any new vendor.

### A clean demo database for a second try

> **Warning: this deletes your stored Gmail connection** (and every run, review item, setting and PO you created). Afterwards you would have to connect Gmail again. Only do this if you accept that.

Stop the backend (Ctrl+C in Terminal 1), then either:

```powershell
cd C:\Zamp_ai_Automation\backend
python -m app.db.reset --demo
python -m app.api.serve --replay ..\data\recordings
```

or, in one step:

```powershell
python -m app.api.serve --replay ..\data\recordings --reset-demo
```

**Without deleting anything,** you can try again on a separate scratch database instead. Point `DATA_DIR` at a new folder; your `data\app.db` and Gmail connection are untouched:

```powershell
cd C:\Zamp_ai_Automation\backend
$env:DATA_DIR = "$env:TEMP\erp_trial"          # outside the repository
python -m app.db.reset --demo
python -m app.api.serve --replay ..\data\recordings
# afterwards, in this terminal: Remove-Item Env:DATA_DIR   (and delete $env:TEMP\erp_trial when done)
```

The scratch database has no Gmail connection and no recordings folder of its own. Replay still reads `..\data\recordings`, which is outside `DATA_DIR`.
