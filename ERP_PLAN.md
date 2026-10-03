# Simulated ERP purchase-order feed: plan (awaiting approval)

Branch `feature/erp-feed`, from `feature/settings` at `f211286`. `master`, `feature/gmail-integration`, `feature/po-export` and `feature/settings` stay unchanged, and nothing is pushed.

**Purpose:** show that larger companies send POs as structured ERP data (SAP / Coupa / Oracle style), so nothing needs extracting. It is a demo stand-in and is labelled **"Simulated ERP (demo)"** everywhere it appears.

**Scope limits:**
- No LLM anywhere and no model cost.
- No new dependency.
- No new service: the mock source is a bundled file read by our own backend.

## What exists today (relevant)

- **One writer.** `app/po/store.save_po` is the only writer of POs. It writes in one transaction: a new vendor (always `new`), the PO (always `open`) and its lines. Provenance goes in `purchase_orders.meta` (JSON), which has only a `json_valid` CHECK.
- **One validator.** `app/po/validate.validate_po(POCreate, conn, settings, new_vendor)` returns errors and warnings, and is shared by the form, text and document paths.
- **Vendor resolution.** The form's draft path uses `resolve_vendor(name, vendors, match, tax_id=)`, which tries the tax ID first.
- **Structural test.** `test_save_po_is_called_from_exactly_one_route_and_only_store_writes_pos` pins `save_po(` to `routes_po.py` only.
- **Gaps in today's validator:**
  - A **negative quantity is not blocked** today (only negative money is).
  - **Line math** (amount ≠ qty × price) and **lines ≠ total** are *warnings*, not errors.
  - An existing PO number is the error `duplicate`.

## 1. Feed format and adapter

**Feed file:** `data/erp_feed_sample.json`, a JSON envelope. Money and quantities are **strings**. JSON numbers are also accepted, but they are parsed with `json.loads(parse_float=Decimal)`, so there are no floats anywhere. Cents conversion goes through `app/money.to_minor`.

```json
{
  "_SIMULATED": "Simulated ERP (demo): hand-written sample feed, not a real ERP export.",
  "format": "simerp.po-feed/v1",
  "system": "SIMERP", "exported_at": "2026-10-03T09:00:00Z",
  "purchase_orders": [{
    "po_number": "4500012345", "buyer_reference": "REQ-7781 / J. Rao", "erp_status": "RELEASED",
    "currency": "USD", "issued_date": "2026-09-28", "total_amount": "1260.00",
    "vendor": {"name": "SuperStore", "tax_id": null,
               "address": {"street": "…", "city": "…", "postal_code": "…", "country": "US"}},
    "lines": [{"line_number": 10, "description": "Ergonomic office chair, black", "quantity": "6",
               "unit_price": "210.00", "unit_of_measure": "EA", "line_amount": "1260.00"}]
  }]
}
```

**ERP status mapping:**
- `RELEASED` and `APPROVED` are importable.
- `DRAFT`, `CANCELLED` and `CLOSED` become a problem ("not released in the ERP").
- An imported PO is always `open` (unchanged rule).

**Fields we have no column for:**
- Unit of measure, buyer reference and the full vendor address go into `meta`.
- A new vendor gets `country` from `address.country`.
- ERP line numbers (10, 20, …) are kept in `meta`. Our `line_no` stays 1..n, as the form does today.

**Adapter design:** this is the only per-ERP code.

```python
class ERPAdapter(Protocol):
    key: str                      # "simerp-v1"
    formats: tuple[str, ...]      # envelope "format" values it reads
    def parse(self, doc: dict) -> list[FeedPO]: ...
```

`FeedPO` is the ERP-neutral middle form. It holds:
- a `POCreate`, as the form posts it, with string values;
- a `FeedVendor` (name, tax_id, country, address);
- `buyer_reference`, `erp_status`, `line_uom`, `erp_line_numbers`;
- `adapter_problems`: structural problems such as a missing field or a non-numeric value, using the form's own error codes.

**Registry:** `ADAPTERS = {"simerp.po-feed/v1": SimErpV1()}`, selected by the envelope's `format`. An unknown format is refused with a message.

**Adding a second ERP:** for example a Coupa-style feed (`"poNumber"`, `"supplier": {"displayName"}`, `"orderLines"`):
1. Add one module `app/erp/adapters/coupa_like.py` that implements `parse`.
2. Register its format.
3. Add a fixture feed and per-adapter mapping tests.

Classification, validation, saving, the API and the UI are shared and do not change.

**Feed source:** `FeedSource.fetch() -> (bytes, display_name)`. v1 is `FileFeedSource(settings.erp_feed_path)`. A real HTTP or SFTP source would be a second class behind the same call.

## 2. Modules, routes, UI

**Backend, all new under `app/erp/`:**

| File | Role |
|---|---|
| `source.py` | Reads the file with a size cap, SHA-256, JSON with Decimal |
| `adapters/` | `base.py`, `simerp_v1.py`, the registry |
| `preview.py` | Classifies each PO. **Read-only.** |
| `importer.py` | Re-runs the preview for the ticked POs, then calls `save_po` |

The routes are in `app/api/routes_erp.py`, behind `ACCESS_TOKEN` through the existing middleware.

**Classification in `preview.py`**, in this order:
1. **Duplicate in the feed:** a PO number appears more than once in the feed. **Every** copy becomes a problem.
2. **Already exists:** an exact `po_number` match in the database. The PO is skipped, with a link to the existing PO, and is **never** compared or updated.
3. **Vendor resolution:** `resolve_vendor`, tax ID first.
   - Exactly one match gives that `vendor_id`.
   - No match gives a `NewVendorIn`, created as status `new`.
   - An **ambiguous** match is a problem: "choose the vendor in the form".
4. **ERP status** (see the mapping in section 1).
5. **Validation:** `validate_po(create, conn, settings, new_vendor)`, the same function and messages as the form.
   - Errors make the PO a **problem**, which can't be ticked.
   - Warnings are shown on a **new** row.

The result is `new | exists | problem`, with `issues` in the form's `{field, level, code, message}` shape. Nothing is written: tests compare whole-database counts and a byte comparison of the DB file.

**Routes:**
- `GET /api/erp/preview` returns `{label: "Simulated ERP (demo)", feed: {name, sha256, exported_at, format, adapter, count, truncated}, pos: [{po_number, class, vendor: {resolved_id | new}, currency, total, lines, issues, existing_po_id}]}`.
- `POST /api/erp/import` with body `{feed_sha256, po_numbers: [...], confirm: true}`.
  - **Refused, with nothing written, when:**
    - `confirm` is missing (400);
    - nothing is picked, or more than the cap (422);
    - a picked number is not in the feed (422 `not_in_feed`);
    - the file changed since the preview (409 `feed_changed`, returning a fresh preview).
  - Otherwise, each pick in feed order is re-classified **against the current database** and, if still `new`, saved through `save_po` (`DuplicatePONumber` is caught and reported).
  - Each pick reports `imported` (with `po_id`), `skipped_exists` or `refused` (with issues). One failure does not stop the others.
  - **Re-resolving per pick** means two feed POs for the same new vendor create that vendor **once**: the second resolves to it by exact name or tax ID.

**Provenance**, passed to `save_po`:

```
{source: "erp", erp_label: "Simulated ERP (demo)", adapter, feed_file, feed_sha256, synced_at,
 buyer_reference, erp_status, erp_vendor: {name, tax_id, address}, erp_line_numbers, line_uom}
```

`entered_at` is still set by `save_po`.

**Frontend:**
- **PO list header:** a **"Sync from ERP (simulated)"** button next to "New purchase order".
- **New screen `/pos/erp-sync`:**
  - A banner: "Simulated ERP (demo): a bundled sample file stands in for an ERP connection. Nothing is saved until you confirm."
  - Three groups, each with a count:
    - **New**: tick-boxes (none pre-ticked, as in the Gmail import), vendor ("existing: X" or "new vendor, status new"), total, lines, warnings;
    - **Already exists (skipped)**: with a link;
    - **Has problems**: with each reason.
  - A button **"Import N purchase orders"**, then a confirm step.
  - The result list links each imported PO.
- **Provenance display:**
  - The PO list's **Entered** column shows "Simulated ERP feed".
  - "Where this PO came from" shows **Entered by: Simulated ERP feed**, the feed file, the sync time, the ERP status, the buyer reference and the units of measure.

## 3. Schema and contract changes

- **No schema change, no v5, no migration.** `meta` is free JSON, and `source: "erp"` needs no CHECK change. Your existing database needs nothing.
- **No change to SPEC §4-6.**
- **SPEC §11 gets new items:**
  - the source is simulated;
  - there is one adapter per ERP;
  - existing POs are never overwritten, because there is no update path at all;
  - no model is involved;
  - the caps;
  - item 72's "only `POST /api/pos` calls `save_po`" is amended.
- **Changed existing assertions:**
  1. `tests/po/test_po_save.py::test_save_po_is_called_from_exactly_one_route_and_only_store_writes_pos`: allowed callers change from `["routes_po.py"]` to `["importer.py", "routes_po.py"]` (each exactly once). The "only `store.py` and `seed.py` write POs" half is **unchanged**.
  2. Any PO-list or PO-page frontend test that pins the Entered wording is expected to be untouched. The new label is an added mapping. Any change will be listed.

## 4. Deployed Render backend

- **Where the file comes from:** the feed ships in the repo at `data/erp_feed_sample.json`, which is not gitignored and is checked by a test. `Settings.erp_feed_path` defaults to `ROOT_DIR / "data" / "erp_feed_sample.json"`, so the file is found in Render's build checkout.
- **Not under `DATA_DIR`:** the path is deliberately **not** moved under `DATA_DIR`. `DATA_DIR` is for writable state, and the feed is read-only bundled input.
- **Override:** `ERP_FEED_PATH` replaces the file. No other config is needed. `ERP_FEED_ENABLED` defaults to true; set false, it hides the button and the routes answer 404.
- **What works on Render:** preview and import, with no extra service and no secret.
- **What doesn't last on Render:** the free instance's database is ephemeral (§11 item 80), so imported POs vanish on spin-down or redeploy. A fresh demo database shows all sample POs as "new" again. Nothing else differs.

## 5. Test plan

| Area | Tests |
|---|---|
| Sample-feed cases | One test per case, each with its class and reason (sample contents listed below) |
| No writes at preview | Every table's row count and the DB file bytes are equal before and after preview; the preview route never calls `save_po` (monkeypatched to fail) |
| Idempotent re-sync | Import all new POs; a second preview shows them as "already exists"; a second import of the same numbers gives `skipped_exists` with zero new rows. Two concurrent imports of the same PO create one row (unique key plus `DuplicatePONumber`) |
| No overwrite | A seeded PO with invoices and ledger entries (PO-SS-005) appears in the feed with different values. Its PO row, lines, ledger, consumption and meta are byte-equal after preview and import |
| Vendors | An existing vendor is matched by name, by alias and by tax ID. A new vendor is created with status `new` and its country. Two POs for one new vendor create one vendor. An ambiguous match is a problem. A blocked vendor gives the form's warning |
| Validation parity | For each case, the preview's issues equal `validate_po` on the same `POCreate` (same codes and messages) |
| Robustness | Unknown format; not JSON; file missing; file over the size cap; envelope without `purchase_orders`; a line that is not an object; a number as a string with letters. Each gives a clean message, never a 500 |
| Access gate | Both routes return 401 without the token and 200 with it |
| Caps | Over `erp_max_pos_per_sync`: the preview is truncated with a count and the import refuses extra picks. Over the line cap: a problem. Over `erp_feed_max_bytes`: refused before parsing |
| Mapping | `simerp-v1` field mapping, money exact to the cent, units of measure and ERP line numbers in `meta`, the registry choice, and the "how to add an adapter" contract via a tiny test-only second adapter |
| Six-invoice regression | The six real invoices on the demo database give the same decision, matched PO, match status and triggered checks: (a) as today and (b) **after importing the whole sample feed**. This proves that the new POs don't steal or confuse matches |
| Frontend (vitest, fixtures recorded from the real endpoints) | Button label; three groups; nothing pre-ticked; problems can't be ticked; confirm step; results; "Simulated ERP" label on the screen, in Entered and in provenance; 401 handling |

**Sample feed contents:**
- **Valid new, existing vendors:** about 3 POs, for SuperStore matched by name and for Electronics Mart India matched by tax ID. They are deliberately unlike the six invoices' items and amounts.
- **Valid new, new vendor:** "Northwind Office Supplies Ltd", which also appears on a second PO so the vendor is created once.
- **Different currency:** a valid PO in EUR.
- **Already exists:** `PO-SS-005`.
- **Missing required field:** no currency.
- **Line math:** a line amount that is not qty × price (see question 2).
- **Negative quantity** (see question 1).
- **Duplicate inside the feed:** the same PO number twice.
- **Not released:** one `CANCELLED` PO.

## 6. Build stages (commit after each; STATUS updated; gate in brackets)

1. **R1: source, adapter, preview.** Settings, the sample feed, `app/erp/source.py`, the adapter and registry, `preview.py`. [Backend: every sample case, no-write, parity, robustness, caps, mapping.]
2. **R2: routes and import.** `routes_erp.py`, `importer.py`, the structural-test change, SPEC §11. [Backend full suite: idempotence, no overwrite, vendors, gate, concurrency, the six-invoice regression (a) and (b).]
3. **R3: UI.** The button, `/pos/erp-sync`, Entered and provenance labels, fixtures with a drift test. [vitest, tsc, build, backend full.]
4. **R4: docs.** README section, SPEC final, STATUS, `ERP_REPORT.md` with browser steps. [All gates, fresh counts.]

## 7. Open questions (recommendation first)

1. **Negative quantity.** The form accepts it today (only negative money is blocked).
   - **Recommend:** block it for the feed only. The ERP contract says quantity > 0, so the adapter reports it as a problem using the form's message style. The manual form stays as it is.
   - Alternative: add it to the shared `validate_po` as an error, which changes the form's behaviour and an existing case.
2. **Warnings in the feed** (line math, lines ≠ total, new or blocked vendor). The form treats them as warnings that can be saved once seen.
   - **Recommend:** for feed POs, **line math and lines ≠ total are problems** (can't be ticked), because a structured ERP record should be internally consistent, and you listed line math as problematic.
   - Vendor warnings (new, blocked, similar) stay warnings on a **new**, tickable row, as in the form.
3. **Ambiguous vendor:** **recommend** treating it as a problem, not guessing. The PO can still be entered through the form.
4. **Caps:**
   - **Recommend:** at most 100 POs per sync, 200 lines per PO (the existing `po_max_lines`), a feed file of at most 1 MB, and at most 100 picks per import.
   - Over the PO cap, the preview shows the first 100 and says how many were left out.
5. **Partial import.** **Recommend:** per-PO outcomes, as in the Gmail import (`save_po` is one transaction per PO), not all-or-nothing.
6. **The `PO-SS-005` look-alike row.** **Recommend:** "already exists" means an **exact** PO number match. A normalised look-alike (e.g. `po-ss-005`) gets the form's "similar" warning on a new row. Should a look-alike be a problem instead?
7. **Units of measure and ERP line numbers** are stored only in `meta` and shown under provenance. **Recommend:** yes. A real column would need schema v5 and is not needed for matching.
8. **Button placement.** **Recommend:** on the PO list only, plus a short line on the PO page's provenance. Not on the dashboard.
9. **Feature switch.** **Recommend** `ERP_FEED_ENABLED`, default true, so a deployment can hide the demo.
