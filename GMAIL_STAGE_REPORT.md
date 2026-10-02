# Gmail import: stage report (stages 1-3)

Branch `feature/gmail-integration`. `master` is unchanged at `6efd185`. Nothing has been pushed. No Google or Anthropic API call was made, and no secret or `.env` value appears here or in any commit. Stage 5 has not been started.

## 1. Stages completed

| Stage | Commit | What it delivered |
|---|---|---|
| 1 | `2111e58` | **Schema v3** (`oauth_credentials`, `gmail_imports`) and `migrate` 2 -> 3 (and 1 -> 2 -> 3), with a backup before each step. Also: Fernet token cipher (`app/gmail/crypto.py`), `python -m app.gmail.keygen [--append-env]`, the Gmail settings, a start-up check that `http://localhost:8000` reaches the server, the `gmail.readonly` scope constant, `data/*.bak` ignored, and SPEC §5, §7 and §11 items 81-83 |
| 2 | `ab3af7c` | **Search.** The `GmailClient` interface (4 methods, nothing that writes) and the labelled FAKE inbox client and fixture. Also: the query allowlist validator and `finalize`, attachment eligibility, search sessions (the importable candidate set), the deterministic injection flag, `GET /api/gmail/status` and `POST /api/gmail/search`, and SPEC item 84 |
| 3 | `c6307af` | **Import.** `POST /api/gmail/import` (candidate-set check, cap of 10, confirmation, budget pre-check, dedupe, the upload's `validate_file`, the same worker queue). Also: `Job.provenance`, the `source_gmail` audit event, `CostTracker.remaining()`, the six-invoice regression module, and SPEC item 85 |

The plan commits that came before the build are `819d4d5` (the plan) and `e330d47` (GMAIL_PLAN.md). The owner's approval note was added to both files in `2111e58`.

## 2. Test results (fresh run, 2026-10-02)

- **Backend:** `python -m pytest -W error` gave **2341 passed, 0 failed, 2 deselected**. The 2 deselected are the existing paid `live` tests, which are skipped by default. Of the 2341, 169 are new: stage 1 added 33, stage 2 added 113, stage 3 added 23, all in `backend/tests/gmail/`.
- **Frontend:** not touched in stages 1-3, so `tsc` was not re-run. `vitest` was run after every stage as a regression check: **85 passed** each time, unchanged.

## 3. Existing test assertions changed

All six are in stage 1 and are caused by the schema version bump. Stages 2 and 3 changed **no** existing assertion.

| # | Test | Change | Why |
|---|---|---|---|
| 1 | `tests/api/test_deploy.py::test_health_is_ok_with_a_good_database_and_says_nothing_sensitive` | `/health` `schema_version` 2 -> 3 | The current schema is v3 |
| 2 | `tests/test_db_init.py` `EXPECTED_TABLES` (used by `test_init_creates_every_section5_table` and `test_init_is_idempotent`) | adds `oauth_credentials`, `gmail_imports` | Two new tables |
| 3 | `tests/test_schema_v2.py::test_a_v1_database_migrates_with_a_byte_identical_backup_and_unchanged_balances` | final version 2 -> `SCHEMA_VERSION` (3) | `migrate` now chains 1 -> 2 -> 3. The backup, balance and backfill assertions are unchanged |
| 4 | `tests/test_schema_v2.py::test_migrating_twice_is_a_no_op` | backups found 1 -> 2 | One backup per step, as the approved plan says |
| 5 | `tests/test_schema_v2.py::test_a_fresh_database_is_v2`, renamed `test_a_fresh_database_has_the_v2_tables` | version 2 -> `SCHEMA_VERSION` | A fresh database is v3. The v2-table check is unchanged |
| 6 | `tests/test_enum_drift.py` `CHECK_TO_ENUM` | adds `("oauth_credentials", "provider") -> OAuthProvider` | The new CHECK must map to an enum (the drift test requires it) |

**`run_started` detail:** stage 3 adds one key, `source` (`upload` or `gmail`). No existing test asserts that detail, so no assertion changed. The frontend reads only `source_file` from it, and that field is unchanged.

`conftest.py` also changed, but no assertion did. It now blanks `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OAUTH_ENCRYPTION_KEY` and `GMAIL_BACKEND` for every non-live test, as it already did for the Anthropic key.

## 4. Six-invoice regression: Gmail import vs upload

Each path ran on its own fresh demo database, using the recorded extract-v5 replies (picked by file hash) and template explanations.
- **Upload path:** `POST /api/runs`.
- **Gmail path:** `POST /api/gmail/search` + `POST /api/gmail/import` from the FAKE inbox. All six were imported in one request; 14021 and 14130 come from one email with two attachments.

`test_the_six_real_invoices_get_identical_results_imported_from_gmail_or_uploaded` asserts that both paths match. To reprint the table: `cd backend; python -m tests.gmail.regression` (no network, no model).

| Invoice | Upload: decision | Gmail: decision | Matched PO | Triggered checks | Identical? |
|---|---|---|---|---|---|
| superstore_10963 | review | review | PO-SS-001 | r_po_found:matched_without_reference | yes |
| superstore_24429 | review | review | PO-SS-002 | r_po_found:matched_without_reference | yes |
| superstore_14021 | review | review | PO-SS-003 | r_po_found:matched_without_reference | yes |
| superstore_14130 | review | review | PO-SS-005 | r_po_found:matched_without_reference | yes |
| superstore_6459 | review | review | PO-SS-004 | r_po_found:matched_without_reference | yes |
| iq_electronics | review | review | PO-IQ-2025-001 | engine_floor:floor_applied, r_extraction_confidence:low_confidence, r_po_found:matched_without_reference | yes |

"Identical" also covers run status, match status, the result count (16 each), invoice total, file hash, source file name, line-match mode, the review item and the absence of a ledger entry. The decisions and POs equal the earlier six-invoice table in STATUS.md.

## 5. Surprises, deviations and unresolved items

**Deviations from the approved plan** (each recorded in SPEC §11 items 81-85):
1. **Ranker not built** (your decision 3). There is no `rank.py` and no `gmail_rank_with_llm` setting. The deterministic injection flag stays.
2. **`already_processed` also checks `gmail_imports` by SHA-256 across emails,** not only `invoices.file_hash`. Without this, picking the same file from two emails in ONE import would queue both: the second run would start before the first had saved its invoice, and would end in a paid duplicate reject. This keeps decision 4's intent (no money spent on a certain duplicate).
3. **`finalize` with only an upper date bound** (`before:` or `older_than:`) adds `after:` = that bound minus 180 days, not `newer_than:180d`. Combining "before last January" with "newer than 180 days" would always return nothing.
4. **Two extra error codes,** `confirm_required` (400) and `nothing_selected` (422), plus two small modules the plan's table did not name: `app/gmail/errors.py` and `app/gmail/callback.py`.
5. **The worker passes `provenance` to the run function only when it is set,** so every existing test double keeps working unchanged.

**Surprises:**
- **Line endings.** Editing files with Python's `write_text` on Windows produced CRLF working-copy files during stage 1. I caught it before the commit and converted to LF, and checked that the index has no CR (`.gitattributes` requires LF). Later edits write bytes explicitly.
- **Settings tolerate your `.env`.** `Settings` already used `extra="ignore"`, so the `GOOGLE_*` lines in your `.env` loaded fine before this work. At start-up, `serve` reports only `OAUTH_ENCRYPTION_KEY` as missing (by name), so both Google settings are present.
- **localhost works.** On this machine, `http://localhost:8000/health` reaches the server's default 127.0.0.1 binding (checked with a real server, and pinned by a socket test).

**Unresolved or deferred** (none blocks stage 5):
- **A run rejected after import keeps its dedupe row.** The `gmail_imports` row is written just before the run is queued. If the worker's ingest later rejects the file (unlikely, because `validate_file` already passed), a re-import is reported as `already_imported`, pointing at a run that never started.
- **"Connected" can show without a usable client.** With `GMAIL_BACKEND=google`, status shows connected only if a credential row exists. No row can exist until stage 6, and until then search answers `not_connected`.
- **Search sessions live in memory,** so a server restart drops them. An import after a restart gets `search_expired` and the user searches again. This matches the plan.
- **The fake search is a subset.** It ignores `larger:` and `smaller:`, and evaluates relative dates against the fixture's fixed `now` (2026-10-01).
- **The preview's "already imported" mark** uses message + part + size. The final dedupe is by SHA-256 at import, because Gmail attachment ids are not stable.
- **Stale notes below the Gmail section in STATUS.md.** That pre-Gmail section is kept as history and still contains the old "schema version 1" note and the leftover "Frontend: 54 passed" line.

## 6. What you need to do before stage 5

1. Install the changed dependencies (`cryptography` is new; `httpx` moved from dev extras to main), from the repo root:
   ```powershell
   cd C:\Zamp_ai_Automation; .\.venv\Scripts\Activate.ps1; pip install -e ".[dev]"
   ```
2. Bring your database to schema v3. Your `data\app.db` is v2, and `serve` refuses it until it is migrated. This makes a backup `data\app.db.v2-<UTC>.bak`, which is gitignored:
   ```powershell
   cd backend; python -m app.db.migrate
   ```
   Alternatively, start the server once with `--reset-demo`, which discards your local runs.
3. Optional now, required before stage 6: generate the token encryption key. This writes it to `.env` without printing it; keep a backup copy of that line.
   ```powershell
   python -m app.gmail.keygen --append-env
   ```
4. Smoke-check the backend that stage 5 will build on, using the labelled fake inbox (no Google, no cost):
   ```powershell
   $env:GMAIL_BACKEND = "fake"; python -m app.api.serve --replay ..\data\recordings
   # in a second window:
   Invoke-RestMethod http://localhost:8000/api/gmail/status
   Invoke-RestMethod http://localhost:8000/api/gmail/search -Method Post -ContentType application/json -Body '{"query":"after:2026/08/01"}'
   ```
   - Expect `backend: fake`, `connected: True`, and 9 emails.
   - `{"query":"in:anywhere"}` should return a 422 saying spam, trash and all mail are never searched.
5. Decide on the two plan deviations in section 5, items 2 and 3 (the cross-email dedupe and the `after:` window). If you disagree with either, I'll change it before stage 5.
6. For stage 5, keep opening the UI at `http://localhost:5173`, not `127.0.0.1:5173`. The OAuth cookie will depend on it in stage 6.
