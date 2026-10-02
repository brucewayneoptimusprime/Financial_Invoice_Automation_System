# SETTINGS PLAN: rules settings with layered defaults, and a Process button for uploads (awaiting approval)

2026-10-03, branch `feature/settings` (from `feature/po-export` at `f4a02dc`). `master`, `feature/gmail-integration` and `feature/po-export` stay unchanged, and nothing is pushed. No application code is written until this plan is approved.

**What exists today (read before planning):**
- **Global settings already exist** as data:
  - the `rules` table (`params`, `enabled`), seeded from `builtin_rules.py`;
  - the `settings` table (`confidence_threshold`).
- **Rules are loaded globally.** `engine/loader.py` builds the facts snapshot (with `RuntimeSettings.confidence_threshold`) at the start of the match stage. `load_rules(conn)` reads the global rules just before validate.
- **Two readers of the threshold:** the engine floor and `r_extraction_confidence` read `ctx.facts.settings.confidence_threshold`.
- **Locked rules:** the engine already refuses to switch off `config.LOCKED_RULE_IDS = {r_duplicate_exact, r_vendor_status}`. The two floors (`engine_floor`, `engine_floor_reference`) are not rules and cannot be disabled.
- **Escalate-only** is enforced by `decide()` (max over triggered results) and pinned by `tests/engine/test_guardrail.py`.

**Principle.** A PO's settings are an **override layer read by the loader after matching.** No evaluator, floor or `decide()` changes. With no PO override stored and the seeded global values, every decision is identical to today's.

## 1. Editable values, ranges, defaults; the tolerance question

| Value | Where the global lives (unchanged) | Range (UI and API reject anything else) | Default (seed) |
|---|---|---|---|
| Tolerance, percent | `r_tolerance_pct.params.pct` | 0.00 to 25.00 %, step 0.01 | 2.00 % |
| Tolerance, absolute amount | `r_tolerance_pct.params.abs` | 0.00 to 1,000,000.00, whole cents (in the invoice's / PO's currency) | 50.00 |
| Tolerance, how the two combine | `r_tolerance_pct.params.mode` | `lesser_of` ("both limits", stricter) or `greater_of` ("either limit") | `lesser_of` |
| Required-field confidence threshold | `settings.confidence_threshold` | 0.50 to 0.99, step 0.01 | 0.80 |
| Near-duplicate window (days between invoice dates) | `r_duplicate_fuzzy.params.days` | 0 to 90, whole days | 7 |
| Near-duplicate amount tolerance | `r_duplicate_fuzzy.params.amount_tolerance` | 0.00 to 10,000.00, whole cents | 0.00 (exact) |
| Rule on/off, 12 rules | `rules.enabled` | on / off | on |

The 12 switchable rules are:
- `r_po_found`, `r_po_ambiguity`, `r_vendor_po_mismatch`, `r_currency_mismatch`;
- `r_tolerance_pct`, `r_arithmetic`, `r_duplicate_fuzzy`;
- `r_required_fields`, `r_extraction_confidence`, `r_document_type`;
- `r_po_status`, `r_po_line_price`.

**Shown but locked**, as a disabled switch with its reason:
- `r_duplicate_exact` and `r_vendor_status`: "locked: an exact duplicate / a blocked vendor must always be caught";
- `engine_floor` and `engine_floor_reference`: "always on: the system's own minimum checks, not a rule".

Severities are not editable in v1.

**"Duplicate similarity threshold" does not exist** (decision 2). The near-duplicate rule matches on same vendor, same amount (± tolerance), dates within N days and a different number. There is no similarity score. The only similarity threshold in the code is vendor-name matching (0.85, `MatchConfig`), which is PO matching, not a rule. I propose the window and the amount tolerance above instead.

**Tolerance semantics, resolved:**
- **The code** (`engine/tolerance.py`): the allowance is `min(floor(balance × pct / 100), abs)` in mode `lesser_of`, the default, or `max(...)` in `greater_of`. An invoice over the PO balance passes only if the excess is within **both** limits, i.e. the smaller one.
- **SPEC §11 item 27** says the same: "both" = `lesser_of` (the default, stricter), "either / larger-of" = `greater_of`.
- **So: within BOTH, the smaller of the two.** This is **not a behaviour change**; the settings only expose it.
- **The UI wording:** "An invoice may exceed the PO balance by at most 2% **and** at most 50.00, whichever is smaller."
- **Example:** with a balance of 6,000.00, 2% is 120.00 and the absolute is 50.00, so the allowance is 50.00.

## 2. Schema v4, migration, and how the loader gets effective settings

**Global defaults keep their current storage** (the `rules` rows and the `settings` table). Editing a global updates those rows, as a database edit would today. New tables hold only the per-PO layer and the change log:

```sql
-- The per-PO override layer. NULL = inherits the global default. Ranges are enforced again by CHECKs (defence in depth).
CREATE TABLE po_settings (
    po_id                  INTEGER PRIMARY KEY REFERENCES purchase_orders(id),
    tolerance_pct          REAL    CHECK (tolerance_pct IS NULL OR tolerance_pct BETWEEN 0 AND 25),
    tolerance_abs_minor    INTEGER CHECK (tolerance_abs_minor IS NULL OR tolerance_abs_minor BETWEEN 0 AND 100000000),
    tolerance_mode         TEXT    CHECK (tolerance_mode IS NULL OR tolerance_mode IN ('lesser_of', 'greater_of')),
    confidence_threshold   REAL    CHECK (confidence_threshold IS NULL OR confidence_threshold BETWEEN 0.5 AND 0.99),
    duplicate_days         INTEGER CHECK (duplicate_days IS NULL OR duplicate_days BETWEEN 0 AND 90),
    duplicate_amount_minor INTEGER CHECK (duplicate_amount_minor IS NULL OR duplicate_amount_minor BETWEEN 0 AND 1000000),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
-- Per-PO rule switches (no row = inherits). A locked rule can never be stored as off.
CREATE TABLE po_rule_switches (
    po_id      INTEGER NOT NULL REFERENCES purchase_orders(id),
    rule_id    TEXT    NOT NULL REFERENCES rules(id),
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (po_id, rule_id),
    CHECK (enabled = 1 OR rule_id NOT IN ('r_duplicate_exact', 'r_vendor_status'))
);
-- The settings change log (decision 1): audit_events' columns, without the run (a settings change has none).
CREATE TABLE settings_events (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('global', 'po')),
    po_id      INTEGER REFERENCES purchase_orders(id),
    key        TEXT NOT NULL,
    old_value  TEXT CHECK (old_value IS NULL OR json_valid(old_value)),
    new_value  TEXT CHECK (new_value IS NULL OR json_valid(new_value)),
    actor      TEXT NOT NULL DEFAULT 'unauthenticated demo user',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((scope = 'global' AND po_id IS NULL) OR (scope = 'po' AND po_id IS NOT NULL))
);
```

Money (the absolute tolerance and the duplicate amount) is stored as integer cents, per the money policy.

**Migration 3 → 4** (the same pattern as before):
- `python -m app.db.migrate` makes a byte-identical backup `.v3-<UTC>.bak`, then creates the three tables in one transaction and sets `user_version = 4`. Older versions chain 1 → 2 → 3 → 4, with one backup per step.
- `init_db` creates v4, so `reset --demo` and the Render build step get v4 directly.
- `serve`, the pipeline CLI and `/health` refuse v3 with the migrate command.
- `reset` drops the new tables, so stored overrides and the change log go too.

**Existing assertions that will change** (all listed in the stage-1 commit):
- `/health` `schema_version` 3 → 4 (`test_deploy`, and the Gmail stage-1 health test);
- `test_db_init` `EXPECTED_TABLES` gains 3 tables;
- `test_stage1_schema_crypto` "fresh database is v3" / "v3 left alone" and its backup counts;
- `test_schema_v2` "migrating twice" backup count 2 → 3;
- `test_enum_drift`: the new `tolerance_mode` and `scope` CHECKs map to new enums;
- **event-count and event-sequence assertions** that the new `settings_applied` event changes (section 4): `test_stage_events`, `test_runner`, any SSE count test. I will list each exact test and assertion in the commit. Decisions, POs and triggered checks do not change.

**How the engine gets effective settings (loader only).**
- `engine/loader.py` gains `load_effective(conn, po_id: int | None) -> Effective(rules, runtime, record)`.
- It reads the global rules and threshold exactly as `load_rules` / `load_facts` do today, then, if `po_id` is given, applies that PO's `po_settings` columns and `po_rule_switches`:
  - into `r_tolerance_pct.params` (`pct`, `abs`, `mode`);
  - into `r_duplicate_fuzzy.params` (`days`, `amount_tolerance`);
  - into each rule's `enabled`;
  - into `RuntimeSettings.confidence_threshold`.
- A locked rule stays on whatever is stored.
- `record` lists every effective value with its source (`default` | `override`).
- The runner calls it **after the match stage**, with `ctx.matched_po.id` only when `match_status == matched`. Otherwise (no PO, ambiguous, low score) it passes `None`, so the global defaults apply.
- The runner swaps the snapshot's runtime settings (`ctx.facts = ctx.facts.model_copy(update={"settings": runtime})`) and passes the effective rules to the unchanged `run_validate_stage`.
- **Nothing else in the engine changes:** no evaluator, the floors and `decide()` stay as they are.
- **The review approve preview and the allocation fit check** (which reuse `r_tolerance_pct`'s params) read the same `load_effective(conn, po_id)` for the item's PO, so a reviewer sees the PO's own tolerance (decision 7).

## 3. API routes and UI

**Routes:** all behind `ACCESS_TOKEN`; GET and POST only (the existing CORS methods). No model, no cost.

| Route | What |
|---|---|
| `GET /api/settings` | The global values, each with its range, step and built-in default; the 14 rules (enabled, locked, reason) and the 2 floors; `po_overrides` (how many POs are custom) |
| `POST /api/settings/global {values?: {key: value}, rules?: {rule_id: bool}, restore?: [key]}` | Validates every value against section 1 (422 naming the field and its range; nothing written if any value is bad), refuses switching a locked rule off (422), writes, and logs one `settings_events` row per value that actually changed. `restore` puts a value back to its built-in default |
| `GET /api/settings/pos?q=&custom=` | The PO list (number, vendor, status, `default` / `custom (N overrides)`), searchable |
| `GET /api/settings/pos/{id}` | For each value: the effective value, `source` (`inherits` / `overridden`) and the global default it would inherit; the same for each rule switch |
| `POST /api/settings/pos/{id} {values?: {key: value \| null}, rules?: {rule_id: bool \| null}}` | `null` = reset to default (deletes the override). Same validation, locked rule and audit rules. 404 for an unknown PO |
| `GET /api/settings/history?scope=&po_id=&limit=` | The change log, newest first |

**UI:**
- **Header gear.** A gear icon button at the top right of the header (`aria-label="Settings"`) opens `/settings`. It is **not shown on the upload screen** (`/invoices`), as asked; everywhere else it is.
- **`/settings`** (`screens/Settings.tsx`):
  - **(a) Global defaults:** a card per group (tolerance, confidence, near-duplicates). Each field shows its range ("0 to 25 %") and validates as you type (min, max, step). There is "Built-in default: 2%" plus "Restore". Save shows what changed.
  - **Rules:** 12 switches; the 2 locked rules and the 2 floors are disabled, with the reason.
  - **"Recent changes":** the last 20 log entries, with "actor: unauthenticated demo user".
  - **(b) Purchase orders:** a search box and a list with a **default** / **custom (N)** chip. Clicking a PO opens its editor.
- **`/settings/pos/:id`** (the PO editor): each value reads **"inherits default (2%)"** or **"overridden: 5%"**, with an input and a **Reset to default** action. Each rule switch reads "inherits (on)" or "overridden: off". Locked rules and floors are shown disabled. Save and Cancel.
- **PO detail page:** a new **"Rules for this PO"** section with the effective values, marked default or custom, and **"Edit rules for this PO"** linking to the same editor.
- **Note shown on both editors:** "Changes apply to invoices processed from now on. Past decisions keep the settings they were made with."

**Feature 2: staged uploads on `/invoices`.**
- Choosing or dropping files no longer uploads them. They go into a **staged list**, one row each: file name, size, type, and a **Remove** button.
- The existing limits are checked as files are added, and nothing is sent:
  - **too large:** over 20 MB, marked "too large";
  - **wrong type:** not PDF / PNG / JPEG by extension or MIME, marked "not a PDF or image";
  - **too many:** above 20 staged files, the extras are not added, with a message.
- **"Process N invoices"**, where N is the number of acceptable files, sends them one request per file, in order, through the existing batch logic. It is disabled at 0. **"Clear"** empties the list.
- **A single file** still opens its live run view after Process (decision 10). With a PO context (`?po=`) the batch view stays, as today.
- The Gmail panel is unchanged, and there is no gear on this screen.

## 4. Recording the effective settings per run; auditing changes

- **Per run.** Right after the match stage, the runner writes one `audit_events` row on the run: `stage = validate`, `event_type = settings_applied`, outcome `info`.
  - **Message:** "Judged under PO-SS-001's settings (all inherited)" / "… (2 overridden: tolerance 5%, near-duplicate window 14 days)" / "Judged under the global defaults (no confidently matched PO)".
  - **Detail:** `{scope: global|po, po_id, po_number, values: {tolerance_pct, tolerance_abs, tolerance_mode, confidence_threshold, duplicate_days, duplicate_amount, rules_enabled{...}}, sources: {key: default|override}}`.
  - **Why it is safe:** audit rows are never updated, so a later change never rewrites an old decision. The run view shows the event in the validate stage ("Settings used"). Changes apply to future runs only.
  - Existing rule results already carry the numbers they used (e.g. the tolerance allowance), so old trails stay self-explanatory.
- **Per change.** One `settings_events` row per changed value: scope, the PO, the key, the old and new values (JSON), `actor = "unauthenticated demo user"`, a message ("Tolerance percent for PO-SS-001: inherits 2.00% → 5.00%"), and the time.
  - A save that changes nothing writes nothing.
  - A reset is logged with `new_value = null` ("reset to default").
  - SPEC §11 notes that there are no user accounts, and that **production must restrict who may change settings** and record a real identity.

## 5. Test plan (offline; `pytest -W error` + vitest)

- **Six-invoice regression, default settings.** With nothing stored, the six real invoices give **exactly** today's decisions, matched POs and triggered checks: all review, PO-SS-001..005 and PO-IQ-2025-001, the same 16 results. The only trail difference is the new `settings_applied` event (scope po, all inherited).
- **Layering:**
  - a global change reaches a PO without an override;
  - a PO override wins over the global;
  - a later global change does not touch an overridden key but reaches the inherited ones;
  - reset returns the key to inheriting, and the row is gone;
  - an ambiguous or unmatched invoice uses the globals even if some PO has overrides.
- **The engine really uses it:**
  - with a hand-written over-balance invoice, PO tolerance 5% gives approve where the global 2% / 50.00 gives review, and vice versa;
  - a PO threshold of 0.95 turns a 0.9-confidence field into review;
  - a PO-disabled `r_po_line_price` is skipped, with the existing `rule_skipped` event;
  - the review preview's fit check uses the PO tolerance.
- **Ranges:** every key below its minimum, above its maximum, at the wrong step (sub-cent, fractional days) or of the wrong type gives 422 naming it, and nothing is written. The DB CHECKs reject a direct bad insert.
- **Locked:**
  - switching `r_duplicate_exact` / `r_vendor_status` off, globally or per PO, gives 422, and the DB CHECK refuses it too;
  - the floors are not switchable;
  - even with a forged row, the engine still runs locked rules (the existing `locked_rule_enabled` event).
- **Escalate-only:** the existing guardrail tests stay. A new property test, over random combinations of in-range effective settings and switches, checks:
  - final severity = max over triggered results;
  - both floors are always present;
  - turning a rule off can only remove that rule's result, never lower any other.
- **Recorded per run:**
  - the `settings_applied` detail equals the effective values;
  - change a setting afterwards and the old run's event and decision are unchanged;
  - the next run shows the new values.
- **Audit:**
  - one row per changed value, with old / new / scope / actor / time;
  - no row for an unchanged save;
  - reset rows;
  - the history route filters by scope and PO;
  - every settings route is behind the access token;
  - no model call.
- **Migration / schema:** 3 → 4 with a backup; rollback on failure; v4 no-op; a fresh v4 database; serve / health refuse v3; reset clears the tables.
- **Frontend (vitest):**
  - the gear is in the header, opens `/settings`, and is absent on `/invoices`;
  - global fields show ranges and refuse out-of-range input before saving;
  - the server's 422 is shown;
  - locked rules and floors are disabled with the reason;
  - the PO list shows default / custom;
  - the editor shows "inherits default (2%)" / "overridden: 5%", and reset sends `null`;
  - the PO page has "Rules for this PO", linking to the editor.
- **Staged upload:**
  - choosing or dropping files makes **no request**;
  - rows show name, size and type;
  - Remove takes a file out;
  - too-large and wrong-type files are marked and never sent;
  - the 20-file cap holds;
  - "Process N invoices" sends exactly the staged acceptable files, in order;
  - a single file opens its run view after Process;
  - the button is disabled at 0.
  - The existing `upload.test.tsx` tests that expected immediate posts gain the Process click; they are listed in the commit.

## 6. Build stages (commit after each; STATUS.md updated; test gate in brackets)

1. **S1: schema v4 and the loader.** `schema_v4.sql`, migrate 3 → 4, `load_effective`, runner wiring, the `settings_applied` event, review preview / allocation on effective tolerance, SPEC §11 items. [Migration tests, layering at the loader level, six-invoice regression, escalate-only property; the full suite green, with the changed assertions listed.]
2. **S2: settings API and audit.** The six routes, validation and ranges, locked rules, `settings_events`. [Range, locked, audit, history, gate, no-writes-on-unchanged tests.]
3. **S3: settings UI.** The header gear, `/settings`, the PO editor, "Rules for this PO" on the PO page. [vitest, `tsc`, build.]
4. **S4: staged upload.** The staged list, Remove, "Process N invoices". [vitest including the updated upload tests.]
5. **S5: docs.** README, STATUS, `SETTINGS_REPORT.md`. Then stop.

## 7. Open questions (with recommendations)

1. **Where settings changes are logged.** `audit_events.run_id` is `NOT NULL` and must point to a run, and settings changes have no run. Each option changes SPEC §5, so I am asking:
   - (A) a new `settings_events` table with `audit_events`' fields plus scope / PO / actor;
   - (B) rebuild `audit_events` with a nullable `run_id`. Every reader of that table (run view, live stream, dashboard) assumes a run.

   **Recommend A.** The per-run "settings used" record still goes into `audit_events`.
2. **"Duplicate similarity threshold"** does not exist. **Recommend exposing the near-duplicate window (days) and amount tolerance instead**, and leaving vendor-name similarity (PO matching) out of v1.
3. **Should the tolerance mode ("both limits" vs "either limit") be editable?** **Recommend yes,** with the default "both" (today's behaviour).
4. **The global absolute tolerance has no currency** (50 means 50 USD or 50 INR). **Recommend keeping today's behaviour,** saying so in the UI, and using PO overrides for per-currency amounts. Per-currency defaults are a later step.
5. **May a PO override be looser than the global?** **Recommend yes, within the ranges.** It is a deliberate human choice, logged, and the engine's max and floors are unaffected. The alternative is to allow only stricter overrides.
6. **"Restore built-in default" per global value?** **Recommend yes:** cheap, and it makes experiments reversible.
7. **The review approve preview and allocation fit check use the PO's effective tolerance?** **Recommend yes,** so the reviewer sees the same allowance the engine used.
8. **Line-price tolerance (`r_po_line_price` pct / abs) editable?** **Recommend not in v1,** to keep the list short. It can be added later the same way.
9. **The gear on every screen except `/invoices`?** **Recommend yes,** as asked.
10. **A single staged file still opens its live run view after Process?** **Recommend yes:** today's flow, one click later.
11. **SPEC changes:** §5 gains `po_settings`, `po_rule_switches` and `settings_events` (asking; see 1); §6 and §8 are unchanged (no contract or action change). §11 gains new items: the layering, ranges, tolerance semantics restated, the per-run record, the change log, "no accounts: production must restrict who may change settings", and **vendor-level overrides as the known next step**.
