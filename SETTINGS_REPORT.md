# Rules settings and staged uploads: build report (S1-S5)

Branch `feature/settings`, created from `feature/po-export` at `f4a02dc`. `master` (`6efd185`), `feature/gmail-integration` (`81cab06`) and `feature/po-export` (`f4a02dc`) are unchanged, and nothing has been pushed. No model call and no Google call are involved. No `.env` value or token was printed, logged or written anywhere: the smoke server ran with an empty `ACCESS_TOKEN` on a scratch database, and its log has no token-like content.

## 1. Commits

| Stage | Commit | What |
|---|---|---|
| plan | `8e29e68` | `SETTINGS_PLAN.md` (approved: 1 A, 2-7 yes, 8 no, 9-11 yes, plus your two additions) |
| S1 | `a69c0f8` | **Schema v4 and the loader.** See below. |
| S2 | `1730706` | **Settings API.** See below. |
| S3 | `97166df` | **Settings UI.** See below. |
| S4 | `e4ee32d` | **Staged uploads.** See below. |
| S5 | the commit that adds this file | **Docs.** README section "Rules settings and staged uploads" and the updated known limitation; the `migrate.py` docstring (1 → 2 → 3 → 4); a test that `migrate` keeps a stored Gmail connection; STATUS; this report |

**S1:**
- **Schema:** `po_settings`, `po_rule_switches` and `settings_events`. Migrate goes 3 → 4 with a backup, init creates v4, and serve and `/health` refuse v3.
- **Catalog:** `app/rulesettings/catalog.py` holds the six values, their ranges and the "looser" direction.
- **Loader:** `engine/loader.load_effective(conn, po_id)` applies the global settings, then the PO's.
- **Runner:** after matching, it uses the matched PO's effective settings and writes one `settings_applied` event. The review preview and allocation use the PO's tolerance.
- **SPEC:** §5, and §11 items 91-93.

**S2:**
- `GET /api/settings` and `POST /api/settings/global`.
- `GET /api/settings/pos` and `GET|POST /api/settings/pos/{id}`, where `null` resets a value to the default.
- `GET /api/settings/history`.
- Validation is all-or-nothing, with a 422 that lists `problems`. Each change writes one `settings_events` row with actor "unauthenticated demo user". The responses carry the "looser than default" flags.
- **SPEC:** §11 item 94.

**S3:**
- The header gear, hidden on `/invoices`.
- `/settings`: the global defaults, rules, PO list and recent changes.
- `/settings/pos/:id`: the PO editor.
- "Rules for this PO" on the PO page.
- "Looser than default" markers.
- "Settings used" on the validate stage card and as a section in the result.
- The run view's `settings_used` field.
- Fixtures recorded from the real endpoints, with a drift test.
- **SPEC:** §11 item 95.

**S4:**
- Choosing or dropping files only stages them.
- Each staged row shows the name, size and type, with a Remove button.
- Files that are too large or are not a PDF or image are marked and never sent. A cap limits how many files can be staged.
- "Process N invoices" sends the acceptable files, and Clear empties the list.
- **SPEC:** §11 item 96.

## 2. Test results (fresh run on the final tree, 2026-10-03)

**Backend:** `python -m pytest -W error` gave **2537 passed, 0 failed, 4 deselected**. The 4 deselected are the `live` tests.

| Stage | New tests | Running total |
|---|---|---|
| Before S1 | | 2476 |
| S1 | +20 | 2496 |
| S2 | +38 | 2534 |
| S3 | +2 (fixture drift and `settings_used` content) | 2536 |
| S4 | 0 (frontend only) | 2536 |
| S5 | +1 (migrate keeps the Gmail connection) | 2537 |

Beyond the per-stage counts, these key tests are included:
- the six-invoice regression with no settings stored, where behaviour is identical;
- the escalate-only property over random settings;
- the engine using the PO's settings, with the globals applying on no match or an ambiguous match;
- the locked rules and floors, enforced in the API and by a DB CHECK.

**Frontend:**
- `npx vitest run`: **160 passed** in 15 files.
  - `src/test/settings.test.tsx` is new, with 19 tests.
  - `src/test/upload.test.tsx` has 5 new tests, and 4 existing ones were changed (section 3).
- `npx tsc --noEmit`: clean.
- `npx vite build`: OK.

**Real-server smoke:** `serve --replay data/recordings` on a scratch demo database in the scratchpad (`DATA_DIR`), port 8765:
- `/health` reported schema 4.
- Global tolerance 2 → 3 returned 200, with 1 change logged.
- Tolerance 30 returned 422 "must be between 0 and 25".
- Switching off `r_vendor_status` returned 422 "is locked".
- PO-SS-001 tolerance 5 returned 200, with 1 override and looser = true. The PO list row showed custom and looser.
- Uploading `invoice_Scot Wooten_10963.pdf` completed the run: decision review, matched PO-SS-001.
- The run's `settings_used` said "Settings used: PO-SS-001's settings (1 overridden: tolerance_pct)." with tolerance_pct 5.0.
- The SSE event stream carried exactly one `settings_applied` frame.
- History showed both changes with actor "unauthenticated demo user".
- `/api/dashboard` returned 200.
- Your `data\app.db` was only read for its schema version: it is still v3 and unchanged.

## 3. Existing test assertions changed

**S1 (backend):** nine changes, all caused by the schema version bump 3 → 4. No decision changed.

1. `tests/api/test_deploy.py::test_health_is_ok_with_a_good_database_and_says_nothing_sensitive`: `schema_version` 3 → 4.
2. `tests/test_db_init.py` `EXPECTED_TABLES` (used by `test_init_creates_every_section5_table` and `test_init_is_idempotent`): +3 tables (`po_settings`, `po_rule_switches`, `settings_events`).
3. `tests/test_enum_drift.py` `CHECK_TO_ENUM`: two new mappings for the two new enums, `po_settings.tolerance_mode` → `ToleranceMode` and `settings_events.scope` → `SettingsScope`.
4. `tests/test_schema_v2.py::test_migrating_twice_is_a_no_op`: backups 2 → 3. The test makes one backup per step, and there are now three steps.
5. `tests/gmail/test_stage1_schema_crypto.py::test_a_v2_database_migrates_to_v3_with_a_byte_identical_backup_and_nothing_else_changed`: the expected message "from schema version 2 to 3" became "2 to {SCHEMA_VERSION}", and `== 3` became `== SCHEMA_VERSION`. The test now migrates to the current version.
6. `…::test_a_v1_database_goes_to_v3_in_one_command_with_one_backup_per_step`: "1 to 3" became "1 to {SCHEMA_VERSION}", and `== 3` became `== SCHEMA_VERSION`, for the same reason.
7. `…::test_a_v3_database_is_left_alone`: backups 1 → 2. A v3 database is no longer current, so it gets one migration step (3 → 4).
8. `…::test_a_fresh_database_is_v3_with_both_gmail_tables` was renamed `test_a_fresh_database_is_current_with_both_gmail_tables`, and `== 3` became `== SCHEMA_VERSION`.
9. `…::test_health_is_503_for_a_v2_database`: the reason "expected 3" became "expected {SCHEMA_VERSION}".

**S2 and S3:** none.
- In S3, the committed frontend fixture `gmail_run_view.json` was re-recorded because the run view gained the `settings_used` key and the Gmail fixture drift test compares shapes. The file is data, not an assertion. The other Gmail fixtures changed only in random ids when regenerated, so they were left as they were.
- The PO page's existing export test passes unchanged: the new "Rules for this PO" section hides itself when the settings reply has an unexpected shape.

**S4 (`frontend/src/test/upload.test.tsx`):** choosing files no longer uploads them, so these four tests changed.

10. **"posts one request per file in order; a rejected file fails alone; each run ends with its own decision":**
    - The test now clicks **Process 3 invoices** after choosing the files.
    - The server-rejected file is now `fake.pdf` instead of `notes.txt`, and the expected posts changed from `["a.pdf", "notes.txt", "c.pdf"]` to `["a.pdf", "fake.pdf", "c.pdf"]`. The reason: a `.txt` file is now caught when it is staged and never sent, so it can no longer exercise a server rejection. The mock server also refuses names starting with `fake`, which keeps a server-side 415 under test.
    - The `.txt` case moved to a new staged test.
11. **"refuses more files than allowed, without uploading any"** was renamed **"stages no more files than allowed (the extras are not added), without uploading any"**.
    - The old behaviour refused the whole selection of 4. Now the first 3 are staged and the alert reads "At most 3 files per upload: 1 not added." (the plan's "the extras are not added").
    - It also checks that `4.pdf` is not listed, that the button says "Process 3 invoices", and that `posts` stays `[]`.
12. **"a single file still goes straight to its live run view"**: the test now asserts no navigation before Process, then clicks Process. The run view still opens (decision 10).
13. **"says matching is automatic and marks invoices that matched another PO"** (upload from a PO page): the test now clicks Process after choosing `x.pdf`. All other expectations are unchanged.

## 4. Deviations from SETTINGS_PLAN

1. **No event-count or event-sequence assertion had to change.** Plan section 2 expected `test_stage_events`, `test_runner` and SSE count tests to change. None of them pins the validate stage's events, so the new `settings_applied` event broke nothing.
2. **The timeline test injects the event into the recorded stream.** The SSE stream fixtures (`ss_10963.sse.txt` etc.) were recorded before v4 and were not re-recorded. The timeline test inserts one `settings_applied` event into the recorded stream, built from the real run view's `settings_used` with the outcome the runner writes (`info`). The same recorded stream without the event is also tested. Both run views are tested too: one with `settings_used`, and a pre-v4 one without it.
3. **The staged cap counts every listed row, marked files included.** Remove frees a place. The plan said "above 20 staged files"; I read marked rows as staged.
4. **Process empties the whole list, marked files included.** They were never sendable. Clear does the same without sending.
5. **The "Process N invoices" button is always visible on `/invoices`.** It reads "Process 0 invoices" and is disabled when nothing is staged, so the step is discoverable.
6. **Rule controls differ between the two screens.** Global rules are checkboxes. PO rules are a three-way select (inherits default / on / off), because a PO also needs "inherit".
7. **Recent changes shows the latest 20.** This applies on Settings and in the PO editor. The full log is `GET /api/settings/history`.
8. **"Rules for this PO" hides itself if the settings request fails,** instead of breaking the PO page.

## 5. Unresolved items

- **I did not look at the screens in a browser.** The UI is covered by vitest and the API by the real-server smoke test, but layout, the gear icon and the phone width are yours to check (section 6).
- **Anyone who can open the UI can change settings.** The actor is always "unauthenticated demo user". SPEC §11 item 92 records that production must restrict this.
- **Not editable, by decision:** vendor-level overrides (recorded as the next step), the line-price tolerance (decision 8), severities and required fields.
- **The PDF/image check on staging uses the name or the MIME type.** The server's magic-byte check still decides; a renamed non-PDF is marked "not accepted" after Process, as before.
- `PROJECT_STATE.md` is still untracked, as before.

## 6. How to check it in the browser

### Step 0: migrate your database (once)

Your `data\app.db` is at schema **v3**, and the new server refuses to start on v3. Stop any running server first, then:

```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1
cd backend
python -m app.db.migrate
```

- It prints "Migrated … from schema version 3 to 4. Backup: …app.db.v3-<timestamp>.bak …".
- It adds the three empty settings tables and changes nothing else. Your Gmail connection, runs, POs and ledger are kept (tested in `test_the_3_to_4_migration_keeps_a_stored_gmail_connection_and_its_imports`).
- Running it again is a no-op.
- **Do NOT use `--reset-demo`** (or `python -m app.db.reset`): it rebuilds the database and deletes your stored Gmail connection.

### Start it

**Terminal 1 (backend):**
```powershell
cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1
cd backend
python -m app.api.serve --replay ..\data\recordings     # no paid calls; or --offline
```

**Terminal 2 (frontend):**
```powershell
cd C:\Zamp_ai_Automation\frontend; npm run dev
```

Open **http://localhost:5173**.

### What to look at

1. **Gear.** A gear icon sits at the top right on the Dashboard, Purchase orders and Review queue pages. It is **not** there on **Invoices**.
2. **Settings** (click the gear):
   - six global values, each with its range and built-in default;
   - the rules list, where `r_duplicate_exact`, `r_vendor_status`, "Engine floor" and the reference floor are ticked, greyed out and have a reason;
   - the purchase orders, each marked "default";
   - Recent changes.
3. **Range check.** Type `30` in "Tolerance over the PO balance (percent)". The field shows "At most 25." and Save stays disabled. Put it back.
4. **Staged upload.** On **Invoices**, choose two PDFs and a `.txt` file:
   - nothing is processed;
   - the list shows the name, size and type of each file, and the `.txt` is marked "not a PDF or image";
   - **Remove** takes one out, and the button reads "Process N invoices";
   - click **Clear**.

### Manual test script: global change, PO override, "Settings used"

With the demo data, use PO **PO-SS-001** and `data\invoices\invoice_Scot Wooten_10963.pdf`. On your own data, pick any invoice and the PO it matches.

1. **Change a global tolerance.**
   - Do this: Settings → "Tolerance over the PO balance (percent)": change `2` to `3`, then **Save global defaults**.
   - Expect: "Saved 1 change…". Recent changes shows "…(global default) changed: 2.00% → 3.00%. · actor: unauthenticated demo user".
2. **Override one PO.**
   - Do this: in the Purchase orders list on Settings, search `PO-SS-001` and click it. In the editor, the percent value reads "inherits default (3.00%)". Type `5` and click **Save rules for this PO**.
   - Expect:
     - it now reads "overridden: 5.00%" with the **looser than default** marker;
     - **Reset to default** appears;
     - back in Settings, the PO's row says "custom (1)" with the marker;
     - on the PO's own page (Purchase orders → PO-SS-001), **Rules for this PO** shows 5.00% "custom" with the marker, plus an "Edit rules for this PO" link.
3. **Upload an invoice against it.**
   - Do this: Invoices → choose `invoice_Scot Wooten_10963.pdf` → **Process 1 invoice**.
   - Expect: the live run view opens.
   - If this file was processed before in your database, the duplicate check flags it. That is expected and does not affect this test.
4. **See "Settings used".**
   - On the **Validate** stage card of the live timeline, a line reads "Settings used: PO-SS-001's settings (1 overridden: tolerance_pct)." Open the card: the event is also in its list.
   - When the run finishes, the result has a **Settings used** section:
     - tolerance_pct 5 is marked "this PO", and the other values are marked "default";
     - "Rules switched off: none";
     - a link "Rules for PO-SS-001" opens the editor.
5. **Optional, invoice with no confident match.** Upload an invoice that matches no PO. Its line reads "Settings used: the global defaults (no confidently matched PO)."
6. **Clean up (optional).** In the PO editor, click **Reset to default** → Save. In Settings, click **Restore** next to the percent value. Both changes appear in Recent changes. Past runs keep the settings they were judged under.
